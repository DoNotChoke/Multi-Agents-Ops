import { execSync } from "node:child_process";
import { createHmac } from "node:crypto";
import { createServer, type IncomingHttpHeaders } from "node:http";
import { expect, test } from "@playwright/test";

const PASSWORD =
  process.env.KEYCLOAK_DEV_USER_PASSWORD ?? "dev-only-user-password";
const WEBHOOK_SECRET =
  process.env.LAB_WEBHOOK_SECRET ?? "dev-only-lab-webhook-secret";
const WEBHOOK =
  "http://127.0.0.1:8001/webhooks/alertmanager/int_01JDEVSEED00000000000000AM";
// make run-api and run-ingest let this port through netguard in dev.
const RECEIVER_PORT = 9599;
// A lab fault (deploy/lab/faults/<name>) instead of a recorded alert, e.g. LAB_FAULT=dependency_slow.
const LAB_FAULT = process.env.LAB_FAULT;
const REPO = "..";

type Delivery = { body: string; headers: IncomingHttpHeaders };

test("an alert becomes an incident that is notified and shown live", async ({
  page,
}) => {
  test.setTimeout(LAB_FAULT ? 12 * 60_000 : 90_000);
  const deliveries: Delivery[] = [];
  const receiver = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      deliveries.push({ body, headers: req.headers });
      res.writeHead(204).end();
    });
  });
  await new Promise<void>((ready) =>
    receiver.listen(RECEIVER_PORT, "127.0.0.1", ready),
  );

  await page.goto("/incidents");
  await page.getByRole("button", { name: "Continue with email" }).click();
  await page.getByLabel(/username or email/i).fill("dev-admin");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  const streaming = page.waitForResponse(
    (r) => r.url().endsWith("/api/v1/stream") && r.status() === 200,
  );
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await streaming;

  // Writes through the BFF carry our own Origin.
  const origin = { origin: "http://localhost:3000" };
  const created = await page.request.post("/api/v1/integrations", {
    headers: origin,
    data: {
      type: "webhook",
      name: `e2e-receiver-${Date.now()}`,
      mode: "direct",
      config: { endpoint: `http://127.0.0.1:${RECEIVER_PORT}/hook` },
    },
  });
  expect(created.status()).toBe(201);
  const channel = await created.json();

  try {
    const service = `e2e-${Date.now()}`;
    const title = `${service} returns 5xx`;
    if (LAB_FAULT) {
      // An open incident of a lab service takes the fault's alerts in, so no new one opens.
      const open = await (await page.request.get("/api/v1/incidents")).json();
      const lab = ["frontend", "checkout", "payment"];
      type Row = {
        incident_id: string;
        status: string;
        service: { name: string } | null;
      };
      const blocking = (open.items as Row[]).filter(
        (i) =>
          !["RESOLVED", "CLOSED"].includes(i.status) &&
          lab.includes(i.service?.name ?? ""),
      );
      expect(
        blocking.map((i) => i.incident_id),
        "resolve these lab-service incidents first",
      ).toEqual([]);
      execSync(`make lab-fault FAULT=${LAB_FAULT}`, { cwd: REPO });
    } else {
      // Recorded from the lab's Alertmanager, with a fresh service so it opens its own incident.
      const sent = await page.request.post(WEBHOOK, {
        headers: { authorization: `Bearer ${WEBHOOK_SECRET}` },
        data: {
          alerts: [
            {
              status: "firing",
              labels: {
                alertname: "HighErrorRate",
                service,
                namespace: "shop",
                severity: "critical",
              },
              annotations: { summary: title },
              startsAt: new Date(Date.now() - 60_000).toISOString(),
              generatorURL: "http://prometheus-server/graph",
            },
          ],
        },
      });
      expect(sent.status()).toBe(200);
    }

    // No reload: the stream brings it in.
    const toasts = page.getByRole("region", { name: "New incidents" });
    const toast = LAB_FAULT
      ? toasts.getByRole("link").first()
      : toasts.getByRole("link", { name: title });
    await expect(toast).toBeVisible({
      timeout: LAB_FAULT ? 10 * 60_000 : 15_000,
    });
    await toast.click();
    await expect(page).toHaveURL(/\/incidents\/inc_[0-9A-Z]{26}$/);
    const incidentId = page.url().split("/").pop() ?? "";

    const timeline = page.getByRole("region", { name: "Timeline" });
    await expect(timeline).toContainText("triaged: first notification");
    // The page updates itself when the notification is recorded.
    await expect(page.getByText(/notified \d{4}-\d{2}-\d{2}/)).toBeVisible();

    const ours = () =>
      deliveries.find(
        (d) => JSON.parse(d.body).incident?.incident_id === incidentId,
      );
    await expect.poll(ours, { timeout: 15_000 }).toBeTruthy();
    const delivery = ours() as Delivery;
    const timestamp = String(delivery.headers["x-opsteam-timestamp"]);
    const expected = createHmac("sha256", channel.webhook_secret)
      .update(`${timestamp}.${delivery.body}`)
      .digest("hex");
    expect(delivery.headers["x-opsteam-signature"]).toBe(`sha256=${expected}`);

    const detail = await (
      await page.request.get(`/api/v1/incidents/${incidentId}`)
    ).json();
    const detect = detail.time_to_detect_seconds;
    test.info().annotations.push({
      type: "time_to_detect",
      description: `${detect} s (${LAB_FAULT ? `lab fault ${LAB_FAULT}` : "recorded alert, started 60 s earlier"})`,
    });
    expect(detect).toBeGreaterThan(0);
  } finally {
    await page.request.delete(
      `/api/v1/integrations/${channel.integration_id}`,
      { headers: origin },
    );
    receiver.close();
    if (LAB_FAULT) execSync("make lab-heal", { cwd: REPO });
  }
});
