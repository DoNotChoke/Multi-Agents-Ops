import { defineConfig } from "@playwright/test";

// Needs `make dev-up lab-up dev-seed` and `make run-api`; the web dev server starts here if absent.
export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  use: { baseURL: "http://localhost:3000", trace: "retain-on-failure" },
  webServer: {
    command: "pnpm dev",
    url: "http://localhost:3000/sign-in",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
