const { defineConfig, devices } = require("@playwright/test");
module.exports = defineConfig({
  testDir: "tests/browser",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL:
      process.env.MYMIAM_TEST_URL || "https://mymiam.176-159-167-168.sslip.io",
    channel: "chrome",
    serviceWorkers: "block",
    locale: "fr-FR",
    timezoneId: "Europe/Paris",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
    {
      name: "mobile",
      use: { ...devices["iPhone 13"], defaultBrowserType: "chromium" },
    },
  ],
  reporter: "list",
});
