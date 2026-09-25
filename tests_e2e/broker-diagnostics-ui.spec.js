// Broker Diagnostics presentation: compact Funds & Margin summary with the
// raw broker response kept (collapsed), a clear endpoint-level Market Data
// message derived from the backend response, and a header pill that means
// "last successful broker API request". /api/account and
// /api/broker/status are mocked so the page is deterministic without Groww.
const { test, expect } = require('@playwright/test');

const CHART_LIBRARY_STUB = `
  (() => {
    const noop = new Proxy(function () {}, { get: (_t, key) => (key === 'then' ? undefined : noop), apply: () => noop });
    window.LightweightCharts = noop;
  })();
`;

const BROKER_STATUS = {
  broker: 'GROWW', apiKeyMasked: 'gw...ABCD', apiSecretMasked: '••••••••••••••••',
  authMode: 'API_KEY_SECRET', autoReauthAvailable: true,
  sessionStatus: 'ACTIVE', connectionStatus: 'CONNECTED',
  sessionCreatedAt: new Date().toISOString(),
  sessionExpiresAt: new Date(Date.now() + 6 * 3600 * 1000).toISOString(),
  sessionExpiryIsEstimated: true, lastValidatedAt: new Date().toISOString(),
  lastSuccessfulRequestAt: new Date().toISOString(), lastError: null,
  credentialsPersisted: true, manualTradingBlocked: false,
  capabilities: { market_data: { status: 'UNAVAILABLE', error: 'Access forbidden for this request.' } },
};

const MARGIN = {
  clear_cash: 125000.5,
  net_margin_used: 2300,
  brokerage_and_charges: 45.75,
  collateral_used: 0,
  collateral_available: 50000,
  adhoc_margin: 0,
  fno_margin_details: { net_fno_margin_used: 1500, span_margin_used: 1000, option_buy_balance_available: 98000 },
  equity_margin_details: { net_equity_margin_used: 800, cnc_balance_available: 124000 },
  commodity_margin_details: { commodity_balance_available: 0 },
};

function accountPayload({ marketData }) {
  return {
    source: 'LIVE BROKER DATA',
    profile: { connected: true, error: null, activeSegments: ['CASH', 'FNO'] },
    margin: MARGIN, marginStatus: { available: true, error: null },
    holdings: [], holdingsStatus: { available: true, error: null },
    positions: [], positionsStatus: { available: true, error: null },
    orders: [], ordersStatus: { available: true, error: null },
    instrumentMaster: { available: true, count: 140000, error: null, fields: ['exchange', 'trading_symbol'] },
    marketData,
  };
}

async function openDiagnostics(page, marketData) {
  await page.route('**/lightweight-charts*', (route) => route.fulfill({ contentType: 'text/javascript', body: CHART_LIBRARY_STUB }));
  await page.route('**/api/broker/status', (route) => route.fulfill({ json: BROKER_STATUS }));
  await page.route('**/api/account', (route) => route.fulfill({ json: accountPayload({ marketData }) }));
  await page.goto('/');
  await page.click('a[href="#account-api"]');
  await expect(page.locator('#diagApiConnection strong')).toHaveText('CONNECTED');
}

const FORBIDDEN_MARKET_DATA = {
  status: 'PERMISSION_DENIED_OR_UNAVAILABLE',
  error: 'Access forbidden for this request.',
  availableMethods: ['get_ltp', 'get_quote', 'get_ohlc'],
};

test.describe('Broker Diagnostics UI', () => {
  test('Funds & Margin shows the key metrics first and keeps the raw response collapsed', async ({ page }) => {
    await openDiagnostics(page, FORBIDDEN_MARKET_DATA);
    const panel = page.locator('#marginData');

    const labels = await panel.locator('.margin-metrics dt').allInnerTexts();
    expect(labels.map((label) => label.toUpperCase())).toEqual(
      ['AVAILABLE CASH', 'NET MARGIN USED', 'COLLATERAL AVAILABLE', 'BROKERAGE & CHARGES'],
    );
    await expect(panel.locator('.margin-metrics dd').first()).toHaveText('₹1,25,000.50');
    const segments = await panel.locator('.margin-segment h4').allInnerTexts();
    expect(segments.map((segment) => segment.toUpperCase())).toEqual(['F&O MARGIN', 'EQUITY MARGIN', 'COMMODITY MARGIN']);
    await expect(panel.locator('.margin-segment').first()).toContainText('Net F&O margin used');

    // Raw section: collapsed by default, but every field the panel showed
    // before is still there once opened.
    const raw = panel.locator('details.raw-response');
    await expect(raw).not.toHaveAttribute('open', '');
    await expect(raw.locator('.kv-grid')).toBeHidden();
    await raw.locator('summary').click();
    const rawKeys = await raw.locator('.kv-grid dt').allInnerTexts();
    for (const key of [...Object.keys(MARGIN), ...Object.keys(MARGIN.fno_margin_details), ...Object.keys(MARGIN.equity_margin_details)]) {
      expect(rawKeys.map((k) => k.toLowerCase())).toContain(key);
    }
  });

  test('Market Data shows the actual error and the affected methods from the backend, with the API still CONNECTED', async ({ page }) => {
    await openDiagnostics(page, FORBIDDEN_MARKET_DATA);
    await page.click('summary:has-text("Instrument Master & Market Data")');
    const alert = page.locator('#instrumentMarketData .capability-alert');

    await expect(alert.locator('.capability-alert-title')).toHaveText(/market data — unavailable/i);
    await expect(alert.locator('.capability-alert-error')).toHaveText('Access forbidden for this request.');
    await expect(alert).toContainText('LTP · Quote · OHLC');
    await expect(alert).toContainText('Live market quotes cannot currently be retrieved.');

    await expect(page.locator('#diagApiConnection strong')).toHaveText('CONNECTED');
    await expect(page.locator('#diagMarketData strong')).toHaveText('UNAVAILABLE');
    await expect(page.locator('#diagMarketDataDetail')).toHaveText('Access forbidden for this request.');
    await expect(page.locator('#brokerStatusPill')).toHaveText('Groww Connected');
  });

  test('affected methods are derived from the response, never hardcoded', async ({ page }) => {
    await openDiagnostics(page, { status: 'PERMISSION_DENIED_OR_UNAVAILABLE', error: 'Forbidden', availableMethods: ['get_ltp'] });
    await page.click('summary:has-text("Instrument Master & Market Data")');
    const alert = page.locator('#instrumentMarketData .capability-alert');

    await expect(alert.locator('dd').first()).toHaveText('LTP');
    await expect(alert).not.toContainText('OHLC');
  });

  test('the header pill means last successful broker API request, not a live market feed', async ({ page }) => {
    await openDiagnostics(page, FORBIDDEN_MARKET_DATA);
    await page.evaluate(() => markBrokerSynced(true));
    const pill = page.locator('#connectionPill');

    await expect(pill).toHaveText(/^Last API success · (just now|\d+s ago)$/);
    await expect(pill).toHaveAttribute('title', /Last successful broker API request/);
    await expect(pill).not.toContainText('Live');
  });
});
