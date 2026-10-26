import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { mockApi, problem, renderWithQuery } from "@/test-utils";
import { IncidentScreen } from "./incident-screen";
import { IncidentsScreen } from "./incidents-screen";
import { OverviewScreen } from "./overview-screen";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const CHECKOUT = {
  incident_id: "inc_01CHECKOUT",
  title: "p95 latency of checkout is 2.4s",
  status: "NEW",
  severity: "high",
  service: { service_id: "svc_01CHK", name: "checkout", environment: "shop" },
  unmapped_service: null,
  opened_at: "2026-10-26T09:00:45Z",
  resolved_at: null,
  event_count: 3,
};
const CART = {
  ...CHECKOUT,
  incident_id: "inc_01CART",
  title: "cart errors",
  status: "RESOLVED",
  severity: "medium",
  service: null,
  unmapped_service: { name: "cart", environment: "shop" },
  resolved_at: "2026-10-26T09:30:00Z",
  event_count: 1,
};
const DETAIL = {
  ...CHECKOUT,
  detected_at: "2026-10-26T09:00:45Z",
  first_notified_at: null,
  started_at: "2026-10-26T09:00:00Z",
  time_to_detect_seconds: 45,
  timeline: [
    {
      from_status: null,
      to_status: "NEW",
      reason: "opened from an alert",
      actor: { type: "system", id: "relay" },
      at: "2026-10-26T09:00:45Z",
    },
  ],
  events: [
    {
      event_id: "evt_01A",
      status: "firing",
      title: "p95 latency of checkout is 2.4s",
      severity_hint: "warning",
      labels: { alertname: "HighLatencyP95", service: "checkout" },
      annotations: { generator_url: "http://prometheus:9090/graph?g0=x" },
      starts_at: "2026-10-26T09:00:00Z",
      ends_at: null,
      received_at: "2026-10-26T09:00:45Z",
    },
  ],
  events_truncated: false,
};

test("the list shows severity, service or the raw unmapped name, and status", async () => {
  mockApi({
    "GET /api/v1/incidents": { items: [CHECKOUT, CART], next_cursor: null },
  });
  renderWithQuery(<IncidentsScreen />);
  const checkout = (await screen.findByText(CHECKOUT.title)).closest("tr")!;
  expect(within(checkout).getByText("High")).toBeDefined();
  expect(within(checkout).getByText("checkout")).toBeDefined();
  expect(within(checkout).getByText("New")).toBeDefined();
  expect(
    within(checkout)
      .getByRole("link", { name: CHECKOUT.title })
      .getAttribute("href"),
  ).toBe("/incidents/inc_01CHECKOUT");
  const cart = screen.getByText("cart errors").closest("tr")!;
  expect(within(cart).getByText("Unmapped")).toBeDefined();
  expect(within(cart).getByText("Resolved")).toBeDefined();
});

test("filters go to the API and more pages load on demand", async () => {
  const calls = mockApi({
    "GET /api/v1/incidents": (request: Request) =>
      new URL(request.url).searchParams.get("cursor")
        ? { items: [CART], next_cursor: null }
        : { items: [CHECKOUT], next_cursor: "inc_01CHECKOUT" },
  });
  renderWithQuery(<IncidentsScreen />);
  await screen.findByText(CHECKOUT.title);
  // Open incidents are the default view.
  expect(calls[0].query.getAll("status")).toContain("NEW");
  expect(calls[0].query.getAll("status")).not.toContain("RESOLVED");
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  expect(await screen.findByText("cart errors")).toBeDefined();
  expect(calls.at(-1)?.query.get("cursor")).toBe("inc_01CHECKOUT");
  fireEvent.change(screen.getByLabelText("Severity"), {
    target: { value: "critical" },
  });
  fireEvent.change(screen.getByLabelText("Status"), {
    target: { value: "all" },
  });
  await vi.waitFor(() => {
    const last = calls.at(-1)!;
    expect(last.query.getAll("severity")).toEqual(["critical"]);
    expect(last.query.getAll("status")).toEqual([]);
  });
});

test("empty and failed lists", async () => {
  mockApi({ "GET /api/v1/incidents": { items: [], next_cursor: null } });
  renderWithQuery(<IncidentsScreen />);
  expect(await screen.findByText("No incidents")).toBeDefined();
  cleanup();
  mockApi({ "GET /api/v1/incidents": problem(500, "http.500", "01BAD") });
  renderWithQuery(<IncidentsScreen />);
  expect((await screen.findByRole("alert")).textContent).toContain(
    "http.500 · 01BAD",
  );
});

test("the detail shows the header, timeline and alerts", async () => {
  mockApi({ "GET /api/v1/incidents/inc_01CHECKOUT": DETAIL });
  renderWithQuery(<IncidentScreen incidentId="inc_01CHECKOUT" />);
  expect(
    await screen.findByRole("heading", { name: DETAIL.title }),
  ).toBeDefined();
  expect(screen.getByText(/detected in 45 s/)).toBeDefined();
  const timeline = within(screen.getByRole("region", { name: "Timeline" }));
  expect(timeline.getByText("opened from an alert")).toBeDefined();
  const alerts = within(screen.getByRole("region", { name: "Alerts" }));
  expect(alerts.getByText("HighLatencyP95")).toBeDefined();
  const source = alerts.getByRole("link", { name: "Source" });
  expect(source.getAttribute("href")).toBe("http://prometheus:9090/graph?g0=x");
  expect(source.getAttribute("rel")).toBe("noopener noreferrer");
});

test("customer text in alerts is shown as text, never as markup or links", async () => {
  const evil = "<img src=x onerror=alert(1)>";
  mockApi({
    "GET /api/v1/incidents/inc_01CHECKOUT": {
      ...DETAIL,
      title: evil,
      events: [
        {
          ...DETAIL.events[0],
          title: evil,
          labels: { alertname: evil, team: "<script>alert(1)</script>" },
          annotations: { generator_url: "javascript:alert(1)", summary: evil },
        },
      ],
    },
  });
  const { container } = renderWithQuery(
    <IncidentScreen incidentId="inc_01CHECKOUT" />,
  );
  expect((await screen.findAllByText(evil)).length).toBeGreaterThan(0);
  expect(screen.getByText("team=<script>alert(1)</script>")).toBeDefined();
  expect(container.querySelector("img, script")).toBeNull();
  expect(screen.queryByRole("link", { name: "Source" })).toBeNull();
});

test("an unmapped incident offers to map it to a service", async () => {
  mockApi({
    "GET /api/v1/incidents/inc_01CART": { ...DETAIL, ...CART },
    "GET /api/v1/services": { items: [] },
  });
  renderWithQuery(<IncidentScreen incidentId="inc_01CART" />);
  expect(
    await screen.findByText(/cart in shop is not in the catalog/),
  ).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "Map to service…" }));
  const dialog = within(screen.getByRole("dialog", { name: "Map to service" }));
  expect(dialog.getByLabelText("service = checkout")).toBeDefined();
});

test("another tenant's incident reads as no access", async () => {
  mockApi({
    "GET /api/v1/incidents/inc_01OTHER": problem(404, "incident.not_found"),
  });
  renderWithQuery(<IncidentScreen incidentId="inc_01OTHER" />);
  expect(await screen.findByText("You don't have access")).toBeDefined();
});

test("overview counts open incidents by severity and lists recent ones", async () => {
  mockApi({
    "GET /api/v1/incidents": { items: [CHECKOUT, CART], next_cursor: null },
    "GET /api/v1/services": {
      items: [{ service_id: "svc_01CHK" }, { service_id: "svc_02" }],
    },
    "GET /api/v1/integrations": {
      items: [{ status: "active" }, { status: "disabled" }],
    },
  });
  renderWithQuery(<OverviewScreen />);
  const open = within(
    await screen.findByRole("region", { name: "Open incidents" }),
  );
  expect(await open.findByText("1")).toBeDefined();
  expect(open.getByText("1 high")).toBeDefined();
  expect(
    within(screen.getByRole("region", { name: "Services" })).getByText("2"),
  ).toBeDefined();
  expect(
    within(screen.getByRole("region", { name: "Integrations" })).getByText(
      "1 of 2 active",
    ),
  ).toBeDefined();
  const recent = within(
    screen.getByRole("region", { name: "Recent incidents" }),
  );
  expect(recent.getByRole("link", { name: CHECKOUT.title })).toBeDefined();
  expect(recent.getByRole("link", { name: "cart errors" })).toBeDefined();
});
