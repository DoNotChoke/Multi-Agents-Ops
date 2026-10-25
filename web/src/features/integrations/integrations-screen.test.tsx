import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { mockApi, problem, renderWithQuery } from "@/test-utils";
import { IntegrationsScreen } from "./integrations-screen";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const LAB = {
  integration_id: "int_01LAB",
  type: "prometheus",
  name: "lab-prometheus",
  mode: "direct",
  status: "active",
  config: {
    endpoint: "http://127.0.0.1:30090",
    metric_mapping: { http_latency_p95: "x" },
  },
  has_credentials: false,
  last_tested_at: null,
  created_at: "2026-10-25T07:00:00Z",
};
const REPORT = {
  ok: true,
  checks: [
    {
      name: "endpoint_allowed",
      ok: true,
      detail: "endpoint passes SSRF checks",
    },
    { name: "credentials_readable", ok: true, detail: "no credentials" },
    { name: "reachable", ok: true, detail: "Prometheus 3.15.1" },
    { name: "query_up", ok: true, detail: "14 scrape targets" },
  ],
};

test("loading, then the empty state with a way forward", async () => {
  mockApi({ "GET /api/v1/integrations": { items: [] } });
  renderWithQuery(<IntegrationsScreen />);
  expect(screen.getByRole("status", { name: "Loading" })).toBeDefined();
  expect(await screen.findByText("No integrations yet")).toBeDefined();
});

test("a failed load shows the problem code and request id", async () => {
  mockApi({
    "GET /api/v1/integrations": problem(
      502,
      "integration.unavailable",
      "01FAIL",
    ),
  });
  renderWithQuery(<IntegrationsScreen />);
  expect((await screen.findByRole("alert")).textContent).toContain(
    "integration.unavailable · 01FAIL",
  );
});

test("Test runs the checks and lists each one", async () => {
  const calls = mockApi({
    "GET /api/v1/integrations": { items: [LAB] },
    "POST /api/v1/integrations/int_01LAB/test": REPORT,
  });
  renderWithQuery(<IntegrationsScreen />);
  fireEvent.click(await screen.findByRole("button", { name: "Test" }));
  const checks = within(
    await screen.findByRole("region", { name: "Connection test" }),
  );
  expect(await checks.findByText("14 scrape targets")).toBeDefined();
  expect(checks.getAllByLabelText("passed")).toHaveLength(4);
  expect(calls.at(-1)).toMatchObject({
    method: "POST",
    path: "/api/v1/integrations/int_01LAB/test",
  });
});

test("adding sends the form, keeps a blank token out, and tests the new integration", async () => {
  const calls = mockApi({
    "GET /api/v1/integrations": { items: [] },
    "POST /api/v1/integrations": LAB,
    "POST /api/v1/integrations/int_01LAB/test": REPORT,
  });
  renderWithQuery(<IntegrationsScreen />);
  fireEvent.click(
    (await screen.findAllByRole("button", { name: "Add integration" }))[0],
  );
  const dialog = within(screen.getByRole("dialog", { name: "Add Prometheus" }));
  fireEvent.change(dialog.getByLabelText("Name"), {
    target: { value: "lab-prometheus" },
  });
  fireEvent.change(dialog.getByLabelText("Endpoint"), {
    target: { value: "http://127.0.0.1:30090" },
  });
  fireEvent.click(dialog.getByRole("button", { name: "Save and test" }));
  await vi.waitFor(() =>
    expect(calls.some((c) => c.path.endsWith("/test"))).toBe(true),
  );
  expect(
    calls.find((c) => c.method === "POST" && c.path === "/api/v1/integrations")
      ?.body,
  ).toEqual({
    type: "prometheus",
    mode: "direct",
    name: "lab-prometheus",
    config: { endpoint: "http://127.0.0.1:30090", metric_mapping: {} },
  });
});

test("a refused endpoint is explained and the typed values stay", async () => {
  mockApi({
    "GET /api/v1/integrations": { items: [] },
    "POST /api/v1/integrations": problem(422, "netguard.blocked"),
  });
  renderWithQuery(<IntegrationsScreen />);
  fireEvent.click(
    (await screen.findAllByRole("button", { name: "Add integration" }))[0],
  );
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.change(dialog.getByLabelText("Name"), {
    target: { value: "meta" },
  });
  fireEvent.change(dialog.getByLabelText("Endpoint"), {
    target: { value: "http://169.254.169.254" },
  });
  fireEvent.click(dialog.getByRole("button", { name: "Save and test" }));
  expect((await dialog.findByRole("alert")).textContent).toContain(
    "Private and cloud metadata addresses are blocked",
  );
  expect((dialog.getByLabelText("Endpoint") as HTMLInputElement).value).toBe(
    "http://169.254.169.254",
  );
});

test("text from the customer server is shown, never interpreted", async () => {
  mockApi({
    "GET /api/v1/integrations": { items: [LAB] },
    "POST /api/v1/integrations/int_01LAB/test": {
      ok: true,
      checks: [
        {
          name: "reachable",
          ok: true,
          detail: "Prometheus <img src=x onerror=alert(1)>",
        },
      ],
    },
  });
  const { container } = renderWithQuery(<IntegrationsScreen />);
  fireEvent.click(await screen.findByRole("button", { name: "Test" }));
  expect(
    await screen.findByText("Prometheus <img src=x onerror=alert(1)>"),
  ).toBeDefined();
  expect(container.querySelector("img")).toBeNull();
});
