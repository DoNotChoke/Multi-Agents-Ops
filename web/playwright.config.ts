import { defineConfig } from "@playwright/test";

// Needs `make dev-up lab-up dev-seed` and `make run-api`; the web dev server starts here if absent.
export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  // One tenant, one user and one dev server for every spec: in parallel they only queue
  // behind each other and time out, and run slower than in a row.
  workers: 1,
  use: { baseURL: "http://localhost:3000", trace: "retain-on-failure" },
  webServer: {
    command: "pnpm dev",
    url: "http://localhost:3000/sign-in",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
