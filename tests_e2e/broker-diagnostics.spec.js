// Broker Diagnostics (renamed from "API"/"API Diagnostics") - verifies the
// renamed navigation and that the 4 top status cards + 5 section tags
// render real, distinct diagnostic states from the backend rather than a
// single blanket "Groww Connected" standing in for all of them.
const { test, expect } = require('@playwright/test');

test.describe('Broker Diagnostics page', () => {
  test('sidebar nav is renamed and the page title is Broker Diagnostics', async ({ page }) => {
    await page.goto('/');
    const navLink = page.locator('a[href="#account-api"]');
    await expect(navLink).toHaveAttribute('title', 'Broker Diagnostics');
    await expect(navLink.locator('small')).toHaveText('Diagnostics');

    await navLink.click();
    await expect(page.locator('#account-api h2')).toHaveText('Broker Diagnostics');
  });

  test('the 4 top status cards show real, distinct Connected/Unavailable/Error states', async ({ page }) => {
    await page.goto('/#account-api');
    // Wait for a real backend response to land (not the "Checking..." placeholder).
    await expect(page.locator('#diagApiConnection strong')).not.toHaveText('Checking…', { timeout: 15_000 });

    const cardIds = ['diagApiConnection', 'diagAccountData', 'diagMarketData', 'diagInstrumentMaster'];
    for (const id of cardIds) {
      const label = (await page.locator(`#${id} strong`).innerText()).trim();
      expect(['CONNECTED', 'UNAVAILABLE', 'ERROR']).toContain(label);
      // Every card must carry a real detail line, never a blank placeholder.
      await expect(page.locator(`#${id}Detail`)).not.toHaveText('');
    }

    // The distinguishing requirement this task exists for: broker session
    // being connected must not be conflated with account-data endpoints
    // actually working - they are rendered as two separate cards with
    // their own independently-derived detail text.
    const apiDetail = await page.locator('#diagApiConnectionDetail').innerText();
    const accountDetail = await page.locator('#diagAccountDataDetail').innerText();
    expect(apiDetail).not.toEqual(accountDetail);
  });

  test('each of the 5 compact sections has a real Available/Partial/Unavailable tag and no leaked "undefined" values', async ({ page }) => {
    await page.goto('/#account-api');
    await expect(page.locator('#diagApiConnection strong')).not.toHaveText('Checking…', { timeout: 15_000 });

    const tagIds = ['diagProfileTag', 'diagMarginTag', 'diagHoldingsPositionsTag', 'diagOrdersTag', 'diagInstrumentTag'];
    for (const id of tagIds) {
      const text = (await page.locator(`#${id}`).innerText()).trim();
      expect(['Available', 'Partial', 'Unavailable']).toContain(text);
    }

    for (const summary of ['Connection & Permissions', 'Funds & Margin', 'Holdings & Positions', 'Orders & Trades', 'Instrument Master & Market Data']) {
      await page.click(`summary:has-text("${summary}")`);
    }
    await page.waitForTimeout(200);
    const bodyText = await page.locator('#account-api').innerText();
    expect(bodyText).not.toContain('undefined');
    expect(bodyText).not.toContain('[object Object]');
  });

  test('never renders a raw API key, secret, or access token on the diagnostics page', async ({ page }) => {
    await page.goto('/#account-api');
    await expect(page.locator('#diagApiConnection strong')).not.toHaveText('Checking…', { timeout: 15_000 });
    for (const summary of ['Connection & Permissions', 'Funds & Margin', 'Holdings & Positions', 'Orders & Trades', 'Instrument Master & Market Data']) {
      await page.click(`summary:has-text("${summary}")`);
    }
    const bodyText = await page.locator('#account-api').innerText();
    // Masked values from token_service always contain this placeholder
    // character - a raw key/secret/token never would.
    expect(bodyText).not.toMatch(/[A-Za-z0-9]{20,}/);
  });
});
