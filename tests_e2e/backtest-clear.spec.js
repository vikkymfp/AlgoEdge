// Clear is purely client-side state (see clearBacktestResults() in
// web/app.js) - there is no Python backend logic behind it, so this
// behavior can only be verified in a real browser, not pytest.
const { test, expect } = require('@playwright/test');

test.describe('Backtest page - Clear button', () => {
  test('Clear removes results/charts/tables, hides exports, fires no request, and preserves selections', async ({ page }) => {
    await page.goto('/#backtest-panel');

    // Set explicit selections that must survive Clear untouched.
    await page.click('#backtestIndexTabs button[data-index-id="bank-nifty"]');
    await page.selectOption('#backtestInterval', '5m');
    await page.fill('#backtestSlippage', '2');
    await page.click('#backtestModeToggle [data-mode="single"]');

    await page.click('#backtestRunButton');
    await page.waitForSelector('.backtest-segment', { timeout: 30_000 });

    // Sanity check: a real result is actually showing before we clear it.
    await expect(page.locator('.backtest-kpi-card').first()).toBeVisible();
    await expect(page.locator('#backtestExportBar')).toBeVisible();
    await expect(page.locator('#backtestMetaRow')).toBeVisible();

    // Clear must never issue a new backtest/export request.
    let backtestRequestFired = false;
    const onRequest = (request) => {
      if (request.url().includes('/api/backtest/')) backtestRequestFired = true;
    };
    page.on('request', onRequest);

    await page.click('#backtestClearButton');
    await page.waitForTimeout(300);
    page.off('request', onRequest);
    expect(backtestRequestFired).toBe(false);

    // Results/KPI cards/charts/tables are gone - back to the initial placeholder.
    await expect(page.locator('.backtest-segment')).toHaveCount(0);
    await expect(page.locator('.backtest-kpi-card')).toHaveCount(0);
    await expect(page.locator('.backtest-equity-chart')).toHaveCount(0);
    await expect(page.locator('.backtest-direction-card')).toHaveCount(0);
    await expect(page.locator('#backtestResult')).toContainText('Run a backtest to see results.');
    await expect(page.locator('#backtestDisclaimer')).toHaveText('');

    // Exports disabled/hidden - no old result can be exported.
    await expect(page.locator('#backtestExportBar')).toBeHidden();
    await expect(page.locator('#backtestMetaRow')).toBeHidden();

    // Symbol/Timeframe/Slippage/Mode selections from before Run are unchanged.
    await expect(page.locator('#backtestIndexTabs button.active')).toHaveAttribute('data-index-id', 'bank-nifty');
    await expect(page.locator('#backtestInterval')).toHaveValue('5m');
    await expect(page.locator('#backtestSlippage')).toHaveValue('2');
    await expect(page.locator('#backtestModeToggle [data-mode="single"]')).toHaveClass(/active/);

    // The Excel/PDF buttons live inside #backtestExportBar, already
    // asserted hidden above - they are not visible/clickable at all, which
    // is the actual "exports disabled" guarantee (not just an unused
    // in-memory flag). toBeHidden() on the button itself confirms this
    // directly rather than attempting an actionability-blocked click.
    await expect(page.locator('#backtestExportXlsx')).toBeHidden();
    await expect(page.locator('#backtestExportPdf')).toBeHidden();
  });
});
