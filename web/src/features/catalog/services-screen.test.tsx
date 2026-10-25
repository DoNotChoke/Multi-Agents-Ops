import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { mockApi, problem, renderWithQuery } from "@/test-utils";
import { ServicesScreen } from "./services-screen";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const CHECKOUT = {
  service_id: "svc_01CHK",
  name: "checkout",
  environment: "shop",
  owner: "team-orders",
  repository: null,
  criticality: "critical",
};
const LAB = {
  integration_id: "int_01LAB",
  type: "prometheus",
  name: "lab-prometheus",
  mode: "direct",
  status: "active",
  config: { endpoint: "http://127.0.0.1:30090" },
  has_credentials: false,
  last_tested_at: null,
  created_at: "2026-10-25T07:00:00Z",
};

test("XSS payloads in names and owners render as plain text", async () => {
  mockApi({
    "GET /api/v1/services": {
      items: [
        {
          ...CHECKOUT,
          name: "<img src=x onerror=alert(1)>",
          owner: "javascript:alert(1)",
        },
      ],
    },
  });
  const { container } = renderWithQuery(<ServicesScreen />);
  expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeDefined();
  expect(screen.getByText("javascript:alert(1)")).toBeDefined();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("a")).toBeNull();
});

test("empty and error states", async () => {
  mockApi({ "GET /api/v1/services": { items: [] } });
  renderWithQuery(<ServicesScreen />);
  expect(await screen.findByText("No services yet")).toBeDefined();
  cleanup();
  mockApi({ "GET /api/v1/services": problem(500, "http.500", "01BAD") });
  renderWithQuery(<ServicesScreen />);
  expect((await screen.findByRole("alert")).textContent).toContain(
    "http.500 · 01BAD",
  );
});

test("editing sends a PATCH and a clash is explained", async () => {
  const calls = mockApi({
    "GET /api/v1/services": { items: [CHECKOUT] },
    "PATCH /api/v1/services/svc_01CHK": problem(409, "service.exists"),
  });
  renderWithQuery(<ServicesScreen />);
  fireEvent.click(await screen.findByRole("button", { name: "Edit checkout" }));
  const dialog = within(screen.getByRole("dialog", { name: "Edit checkout" }));
  fireEvent.change(dialog.getByLabelText(/^Environment/), {
    target: { value: "prod" },
  });
  fireEvent.click(dialog.getByRole("button", { name: "Save" }));
  expect((await dialog.findByRole("alert")).textContent).toContain(
    "already exists",
  );
  expect(calls.find((c) => c.method === "PATCH")?.body).toMatchObject({
    name: "checkout",
    environment: "prod",
  });
});

test("import adds only the picked suggestions; existing ones are locked", async () => {
  const calls = mockApi({
    "GET /api/v1/services": { items: [] },
    "GET /api/v1/integrations": { items: [LAB] },
    "POST /api/v1/services/import/prometheus": {
      truncated: false,
      suggestions: [
        { name: "checkout", environment: "shop", exists: false },
        { name: "payment", environment: "shop", exists: true },
        { name: "kube-dns", environment: "kube-system", exists: false },
      ],
    },
    "POST /api/v1/services": (_r: Request, body: unknown) => ({
      ...CHECKOUT,
      ...(body as object),
    }),
  });
  renderWithQuery(<ServicesScreen />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Import services" }),
  );
  const drawer = within(
    screen.getByRole("dialog", { name: "Import services" }),
  );
  await drawer.findByRole("option", { name: "lab-prometheus" });
  fireEvent.click(drawer.getByRole("button", { name: "Find services" }));
  const payment = (await drawer.findByLabelText(
    "payment in shop",
  )) as HTMLInputElement;
  expect(payment.disabled && payment.checked).toBe(true);
  fireEvent.click(drawer.getByLabelText("checkout in shop"));
  fireEvent.click(drawer.getByRole("button", { name: "Add services" }));
  await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(
    calls.find((c) => c.path.endsWith("/import/prometheus"))?.body,
  ).toEqual({
    integration_id: "int_01LAB",
  });
  expect(
    calls
      .filter((c) => c.method === "POST" && c.path === "/api/v1/services")
      .map((c) => c.body),
  ).toEqual([{ name: "checkout", environment: "shop" }]);
});
