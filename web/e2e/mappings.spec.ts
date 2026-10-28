import { expect, test } from "@playwright/test";

const PASSWORD =
  process.env.KEYCLOAK_DEV_USER_PASSWORD ?? "dev-only-user-password";
const WEBHOOK_SECRET =
  process.env.LAB_WEBHOOK_SECRET ?? "dev-only-lab-webhook-secret";
// The lab Alertmanager integration that `make dev-seed` creates; needs `make run-ingest`.
const WEBHOOK =
  "http://127.0.0.1:8001/webhooks/alertmanager/int_01JDEVSEED00000000000000AM";

test("map an unmapped incident to a catalog service", async ({
  page,
  request,
}) => {
  const service = `cart-${Date.now()}`;
  const sent = await request.post(WEBHOOK, {
    headers: { authorization: `Bearer ${WEBHOOK_SECRET}` },
    data: {
      alerts: [
        {
          status: "firing",
          labels: { alertname: "HighErrorRate", service, namespace: "shop" },
          annotations: { summary: `${service} returns 5xx` },
          startsAt: new Date().toISOString(),
        },
      ],
    },
  });
  expect(sent.status()).toBe(200);

  await page.goto("/incidents");
  await page.getByRole("button", { name: "Continue with email" }).click();
  await page.getByLabel(/username or email/i).fill("dev-admin");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  // Sign-in lands on Overview.
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page.goto("/incidents");

  // The relay picks the event up within a second or so; reload until it shows.
  const title = page.getByRole("link", { name: `${service} returns 5xx` });
  await expect(async () => {
    await page.reload();
    await expect(title).toBeVisible({ timeout: 1000 });
  }).toPass({ timeout: 15_000 });
  await title.click();
  await expect(
    page.getByText(`${service} in shop is not in the catalog`),
  ).toBeVisible();

  await page.getByRole("button", { name: "Map to service…" }).click();
  const dialog = page.getByRole("dialog", { name: "Map to service" });
  await dialog
    .getByLabel("Service", { exact: true })
    .selectOption({ label: "checkout · shop" });
  await dialog.getByRole("button", { name: "Save mapping" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText(/is not in the catalog/)).toBeHidden();
  await expect(
    page.locator("p", { hasText: "opened" }).getByText("checkout"),
  ).toBeVisible();

  // Leave the dev tenant as it was: drop the rule; writes through the BFF carry our Origin.
  const rules = await (
    await page.request.get("/api/v1/service-mappings")
  ).json();
  const rule = rules.items.find(
    (m: { matchers: Record<string, string> }) => m.matchers.service === service,
  );
  const removed = await page.request.delete(
    `/api/v1/service-mappings/${rule.mapping_id}`,
    { headers: { origin: "http://localhost:3000" } },
  );
  expect(removed.status()).toBe(204);
});
