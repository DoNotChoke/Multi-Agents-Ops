import { expect, test } from "@playwright/test";

const PASSWORD =
  process.env.KEYCLOAK_DEV_USER_PASSWORD ?? "dev-only-user-password";
const WEBHOOK_SECRET =
  process.env.LAB_WEBHOOK_SECRET ?? "dev-only-lab-webhook-secret";
// The lab Alertmanager integration that `make dev-seed` creates; needs `make run-ingest`.
const WEBHOOK =
  "http://127.0.0.1:8001/webhooks/alertmanager/int_01JDEVSEED00000000000000AM";

test("a new incident appears live, without a reload", async ({
  page,
  request,
}) => {
  await page.goto("/incidents");
  await page.getByRole("button", { name: "Continue with email" }).click();
  await page.getByLabel(/username or email/i).fill("dev-admin");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(
    page.getByRole("link", { name: /new incident|No new incidents/ }),
  ).toBeVisible();

  // The alert arrives after the page has loaded, so only the stream can show it.
  const service = `live-${Date.now()}`;
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

  const toast = page
    .getByRole("region", { name: "New incidents" })
    .getByRole("link", { name: `${service} returns 5xx` });
  await expect(toast).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByRole("link", { name: "1 new incident" }),
  ).toBeVisible();
  await toast.click();
  await expect(
    page.getByRole("heading", { name: `${service} returns 5xx` }),
  ).toBeVisible();
});
