// Playwright Test config for AlgoEdge's browser-only E2E tests (tests_e2e/).
// Auto-starts the real FastAPI dashboard server (the same one `python -m
// algoedge.web_server` starts manually) against real backend endpoints -
// these tests exercise actual client-side behavior in a real browser, not
// mocked DOM, and reuse an already-running dev server on 5173 if present.
const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests_e2e',
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: '.venv/Scripts/python.exe -m algoedge.web_server',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: true,
    timeout: 30_000,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
