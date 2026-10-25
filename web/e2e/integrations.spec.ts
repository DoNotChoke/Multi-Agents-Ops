import { expect, test } from "@playwright/test";

const PASSWORD =
  process.env.KEYCLOAK_DEV_USER_PASSWORD ?? "dev-only-user-password";
const LAB_PROMETHEUS = "http://127.0.0.1:30090";

test("sign in, add the lab Prometheus, and see every check pass", async ({
  page,
}) => {
  await page.goto("/integrations");
  await expect(page).toHaveURL(/\/sign-in$/);
  await page.getByRole("button", { name: "Continue with email" }).click();

  await page.getByLabel(/username or email/i).fill("dev-admin");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();

  await page.getByRole("link", { name: "Integrations" }).click();
  await page.getByRole("button", { name: "Add integration" }).first().click();
  const name = `e2e-prometheus-${Date.now()}`;
  const dialog = page.getByRole("dialog", { name: "Add Prometheus" });
  await dialog.getByLabel("Name", { exact: true }).fill(name);
  await dialog.getByLabel("Endpoint", { exact: true }).fill(LAB_PROMETHEUS);
  await dialog.getByRole("button", { name: "Save and test" }).click();

  const checks = page.getByRole("region", { name: "Connection test" });
  await expect(checks.getByLabel("passed")).toHaveCount(4);
  await expect(checks).toContainText("scrape targets");
  await expect(page.getByRole("heading", { name })).toBeVisible();

  // Leave the dev tenant as it was; writes through the BFF must carry our own Origin.
  const id = (await page.getByText(/^int_[0-9A-Z]{26}$/).textContent()) ?? "";
  const removed = await page.request.delete(`/api/v1/integrations/${id}`, {
    headers: { origin: "http://localhost:3000" },
  });
  expect(removed.status()).toBe(204);
});
