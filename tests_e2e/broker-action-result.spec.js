// API Management: the result of an update/test action is shown separately
// from the current connection state, and is re-checked against it on every
// status refresh - a green "Connected" must never outlive the connection.
// /api/broker/* is mocked so each scenario is deterministic without Groww.
const { test, expect } = require('@playwright/test');

const CONNECTED = {
  broker: 'GROWW', apiKeyMasked: 'gw...ABCD', apiSecretMasked: '••••••••••••••••',
  authMode: 'API_KEY_SECRET', autoReauthAvailable: true, capabilities: {},
  sessionStatus: 'ACTIVE', connectionStatus: 'CONNECTED',
  sessionCreatedAt: new Date().toISOString(),
  sessionExpiresAt: new Date(Date.now() + 6 * 3600 * 1000).toISOString(),
  sessionExpiryIsEstimated: true,
  lastValidatedAt: new Date().toISOString(), lastSuccessfulRequestAt: new Date().toISOString(),
  lastError: null, credentialsPersisted: true, manualTradingBlocked: false,
};
const FORBIDDEN = {
  ...CONNECTED, sessionStatus: 'UNAVAILABLE', connectionStatus: 'ERROR',
  lastError: 'Access forbidden for this request', manualTradingBlocked: true,
};
const EXPIRED = {
  ...CONNECTED, sessionStatus: 'EXPIRED', connectionStatus: 'TOKEN_EXPIRED',
  lastError: 'Authentication failed', manualTradingBlocked: true,
};

// Charts are irrelevant here; a no-op stand-in keeps these tests
// independent of the chart library's CDN being reachable.
const CHART_LIBRARY_STUB = `
  (() => {
    const noop = new Proxy(function () {}, { get: (_t, key) => (key === 'then' ? undefined : noop), apply: () => noop });
    window.LightweightCharts = noop;
  })();
`;

async function mockBroker(page, initialStatus, { persisted = true } = {}) {
  const state = { status: initialStatus };
  await page.route('**/lightweight-charts*', (route) => route.fulfill({ contentType: 'text/javascript', body: CHART_LIBRARY_STUB }));
  await page.route('**/api/broker/status', (route) => route.fulfill({ json: state.status }));
  await page.route('**/api/broker/history**', (route) => route.fulfill({ json: { events: [] } }));
  for (const path of ['access-token', 'credentials']) {
    await page.route(`**/api/broker/${path}`, (route) => {
      state.status = CONNECTED;
      return route.fulfill({ json: { ...CONNECTED, update: { persisted } } });
    });
  }
  await page.route('**/api/broker/reauthenticate', (route) => {
    state.status = CONNECTED;
    return route.fulfill({ json: CONNECTED });
  });
  await page.route('**/api/broker/test-connection', (route) => {
    state.status = CONNECTED;
    return route.fulfill({ json: { connected: true, message: 'Connected' } });
  });
  return state;
}

async function openApiManagement(page) {
  await page.goto('/');
  await page.click('a[href="#broker-panel"]');
  await expect(page.locator('#broker-panel')).toBeVisible();
}

async function updateAccessToken(page) {
  await page.click('#brokerUpdateTokenButton');
  await page.fill('#brokerAccessTokenInput', 'a-token');
  await page.click('#brokerTokenSubmit');
}

// The dashboard polls /api/broker/status on a timer; trigger that same
// refresh directly instead of waiting for it.
const refreshStatus = (page) => page.evaluate(() => loadBrokerStatus());

const result = (page) => page.locator('#brokerActionResult');

test.describe('API Management action result vs current connection state', () => {
  test('successful token save followed by a connection failure never keeps showing Connected', async ({ page }) => {
    const state = await mockBroker(page, FORBIDDEN);
    await openApiManagement(page);
    await updateAccessToken(page);

    await expect(result(page)).toHaveText('Session token validated and saved. Connected.');
    await expect(result(page)).toHaveClass(/positive/);

    state.status = FORBIDDEN;
    await refreshStatus(page);

    await expect(result(page)).toHaveText('Credentials saved, but broker connection is currently unavailable.');
    await expect(result(page)).not.toHaveClass(/positive/);
    await expect(result(page)).not.toContainText('Connected');
    await expect(page.locator('#brokerStatusPill')).toHaveText('Groww Disconnected');
    await expect(page.locator('#brokerConnectionTag')).toHaveText('ERROR');
    await expect(page.locator('#brokerTokenStatus')).toHaveText('UNAVAILABLE');
    await expect(page.locator('#brokerTokenTimeRemaining')).toHaveText('—');
    await expect(page.locator('#brokerLastError')).toHaveText('Access forbidden for this request');
  });

  test('connection loss after a successful validation replaces the stale success message', async ({ page }) => {
    const state = await mockBroker(page, CONNECTED);
    await openApiManagement(page);
    await page.click('#brokerTestConnectionButton');
    await expect(result(page)).toHaveText('Connected — Connected');

    state.status = EXPIRED;
    await refreshStatus(page);

    await expect(result(page)).toHaveText('Broker connection is currently unavailable — see Current API status below.');
    await expect(result(page)).not.toHaveClass(/positive/);
    await expect(page.locator('#brokerStatusPill')).toHaveText('Session Expired');
  });

  test('a stale success message stays cleared even after the connection comes back', async ({ page }) => {
    const state = await mockBroker(page, CONNECTED);
    await openApiManagement(page);
    await updateAccessToken(page);
    await expect(result(page)).toHaveText('Session token validated and saved. Connected.');

    state.status = EXPIRED;
    await refreshStatus(page);
    await expect(result(page)).toHaveText('Credentials saved, but broker connection is currently unavailable.');

    // Reconnected by something other than this action: the old action's
    // validation is not the current truth, so no green "Connected" returns.
    state.status = CONNECTED;
    await refreshStatus(page);
    await expect(result(page)).toHaveText('Credentials saved.');
    await expect(result(page)).not.toHaveClass(/positive/);
    await expect(page.locator('#brokerStatusPill')).toHaveText('Groww Connected');
  });

  test('an update that could not be persisted never claims it was saved', async ({ page }) => {
    const state = await mockBroker(page, CONNECTED, { persisted: false });
    await openApiManagement(page);
    await updateAccessToken(page);
    await expect(result(page)).toHaveText('Session token validated but not saved (kept until the app restarts). Connected.');

    state.status = FORBIDDEN;
    await refreshStatus(page);
    await expect(result(page)).toHaveText(
      'Credentials applied until the app restarts (not saved), but broker connection is currently unavailable.',
    );
  });

  test('the session is shown as generated from the API key & secret, never as a credential', async ({ page }) => {
    const state = await mockBroker(page, EXPIRED);
    await page.goto('/');
    await page.click('a[href="#broker-panel"]');

    const credentials = page.locator('.broker-fact-list').first();
    await expect(credentials).not.toContainText('Access Token');
    await expect(page.locator('#brokerAuthMode')).toHaveText('Generated from API key & secret — renewed automatically');
    await expect(page.locator('#brokerStatusPill')).toHaveText('Session Expired');
    await expect(page.locator('#brokerTokenStatus')).toHaveText('EXPIRED');

    await page.click('#brokerReauthButton');
    await expect(result(page)).toHaveText('New session validated and saved. Connected.');
    await expect(page.locator('#brokerStatusPill')).toHaveText('Groww Connected');

    state.status = { ...EXPIRED, authMode: 'MANUAL_TOKEN', autoReauthAvailable: false };
    await refreshStatus(page);
    await expect(page.locator('#brokerAuthMode')).toHaveText('Pasted session token — not renewed automatically');
    await expect(page.locator('#brokerReauthButton')).toBeHidden();
  });

  test('a working session past the estimated reset shows RENEWAL DUE, not expired or disconnected', async ({ page }) => {
    const renewalDue = {
      ...CONNECTED, sessionStatus: 'RENEWAL_DUE',
      sessionExpiresAt: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
    };
    const state = await mockBroker(page, renewalDue);
    await page.goto('/');
    await page.click('a[href="#broker-panel"]');

    await expect(page.locator('#brokerConnectionTag')).toHaveText('CONNECTED');
    await expect(page.locator('#brokerTokenStatus')).toHaveText('RENEWAL DUE');
    await expect(page.locator('#brokerStatusPill')).toHaveText('Groww Connected');
    await expect(page.locator('#brokerTokenTimeRemaining')).toHaveText(/^Estimated reset passed \d+m ago$/);
    await expect(page.locator('#broker-panel')).not.toContainText('Session Expired');

    // Only an actual authentication failure turns it into an expiry.
    state.status = EXPIRED;
    await refreshStatus(page);
    await expect(page.locator('#brokerStatusPill')).toHaveText('Session Expired');
    await expect(page.locator('#brokerTokenStatus')).toHaveText('EXPIRED');
  });
});
