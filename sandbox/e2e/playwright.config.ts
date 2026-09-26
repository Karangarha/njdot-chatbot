import { defineConfig, devices } from "@playwright/test";
import { env } from "./tests/support/env";

// One worker, files in order: later specs reuse accounts and data created by
// earlier ones, and the review run is heavy enough that parallel runs would
// just queue on the backend anyway.
export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  globalSetup: "./tests/support/global-setup.ts",
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "playwright-report" }],
    ["json", { outputFile: "results.json" }],
  ],
  use: {
    baseURL: env.frontendUrl,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
        // Optional: use an already-installed Chromium instead of the one
        // `playwright install` downloads (e.g. in CI images).
        launchOptions: process.env.PW_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PW_CHROMIUM_EXECUTABLE } : {},
      },
    },
  ],
});
