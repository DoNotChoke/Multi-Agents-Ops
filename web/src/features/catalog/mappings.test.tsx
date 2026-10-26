import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { mockApi, problem, renderWithQuery } from "@/test-utils";
import { MapServiceDialog, MappingsCard } from "./mappings";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const CHECKOUT = {
  service_id: "svc_01CHK",
  name: "checkout",
  environment: "shop",
  owner: null,
  repository: null,
  criticality: "high",
};
const MAPPING = {
  mapping_id: "rule_01A",
  matchers: { service: "cart", namespace: "shop" },
  service: { service_id: "svc_01CHK", name: "checkout", environment: "shop" },
  priority: 0,
  created_at: "2026-10-26T10:00:00Z",
};
const LABELS = {
  alertname: "HighErrorRate",
  service: "cart",
  namespace: "shop",
  severity: "warning",
};

test("the dialog preselects the service labels and files the incident", async () => {
  const calls = mockApi({
    "GET /api/v1/services": { items: [CHECKOUT] },
    "POST /api/v1/service-mappings": MAPPING,
  });
  const onDone = vi.fn();
  renderWithQuery(
    <MapServiceDialog
      incidentId="inc_01CART"
      labels={LABELS}
      onClose={onDone}
    />,
  );
  const dialog = within(screen.getByRole("dialog", { name: "Map to service" }));
  expect(
    (dialog.getByLabelText("service = cart") as HTMLInputElement).checked,
  ).toBe(true);
  expect(
    (dialog.getByLabelText("namespace = shop") as HTMLInputElement).checked,
  ).toBe(true);
  expect(
    (dialog.getByLabelText("alertname = HighErrorRate") as HTMLInputElement)
      .checked,
  ).toBe(false);
  await dialog.findByRole("option", { name: "checkout · shop" });
  fireEvent.change(dialog.getByLabelText("Service"), {
    target: { value: "svc_01CHK" },
  });
  fireEvent.click(dialog.getByRole("button", { name: "Save mapping" }));
  await vi.waitFor(() => expect(onDone).toHaveBeenCalled());
  expect(calls.find((c) => c.method === "POST")?.body).toEqual({
    matchers: { service: "cart", namespace: "shop" },
    service_id: "svc_01CHK",
    priority: 0,
    incident_id: "inc_01CART",
  });
});

test("at least one label must stay selected", async () => {
  mockApi({ "GET /api/v1/services": { items: [CHECKOUT] } });
  renderWithQuery(
    <MapServiceDialog
      incidentId="inc_01CART"
      labels={LABELS}
      onClose={() => {}}
    />,
  );
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.click(dialog.getByLabelText("service = cart"));
  fireEvent.click(dialog.getByLabelText("namespace = shop"));
  expect(
    (dialog.getByRole("button", { name: "Save mapping" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

test("a duplicate rule is explained", async () => {
  mockApi({
    "GET /api/v1/services": { items: [CHECKOUT] },
    "POST /api/v1/service-mappings": problem(409, "service_mapping.exists"),
  });
  renderWithQuery(
    <MapServiceDialog
      incidentId="inc_01CART"
      labels={LABELS}
      onClose={() => {}}
    />,
  );
  const dialog = within(screen.getByRole("dialog"));
  await dialog.findByRole("option", { name: "checkout · shop" });
  fireEvent.change(dialog.getByLabelText("Service"), {
    target: { value: "svc_01CHK" },
  });
  fireEvent.click(dialog.getByRole("button", { name: "Save mapping" }));
  expect((await dialog.findByRole("alert")).textContent).toContain(
    "service_mapping.exists",
  );
});

test("mappings are listed as text and can be deleted", async () => {
  const evil = {
    ...MAPPING,
    matchers: { team: "<img src=x onerror=alert(1)>" },
  };
  const calls = mockApi({
    "GET /api/v1/service-mappings": {
      items: [MAPPING, { ...evil, mapping_id: "rule_01B" }],
    },
    "DELETE /api/v1/service-mappings/rule_01A": new Response(null, {
      status: 204,
    }),
  });
  const { container } = renderWithQuery(<MappingsCard />);
  const card = within(await screen.findByRole("region", { name: "Mappings" }));
  expect(await card.findByText("service=cart")).toBeDefined();
  expect(card.getByText("team=<img src=x onerror=alert(1)>")).toBeDefined();
  expect(container.querySelector("img")).toBeNull();
  fireEvent.click(card.getAllByRole("button", { name: /Delete mapping/ })[0]);
  await vi.waitFor(() =>
    expect(calls.some((c) => c.method === "DELETE")).toBe(true),
  );
});

test("no mappings yet", async () => {
  mockApi({ "GET /api/v1/service-mappings": { items: [] } });
  renderWithQuery(<MappingsCard />);
  expect(await screen.findByText("No mappings")).toBeDefined();
});
