import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { mockApi, renderWithQuery } from "@/test-utils";
import { LiveIncidents } from "./live";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

class FakeEventSource {
  static last: FakeEventSource;
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (m: MessageEvent<string>) => void>();
  closed = false;

  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  addEventListener(type: string, listener: (m: MessageEvent<string>) => void) {
    this.listeners.set(type, listener);
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data: unknown, id = "1") {
    this.listeners.get(type)?.(
      new MessageEvent(type, { data: JSON.stringify(data), lastEventId: id }),
    );
  }
  fail(state: number) {
    this.readyState = state;
    this.onerror?.();
  }
}

const INCIDENT = {
  incident_id: "inc_01NEW",
  title: "checkout returns 5xx <img src=x onerror=alert(1)>",
  status: "NEW",
  severity: "critical",
  service: { service_id: "svc_1", name: "checkout", environment: "shop" },
  unmapped_service: null,
  opened_at: "2026-10-29T09:00:00Z",
  resolved_at: null,
  event_count: 1,
};
const OPENED = {
  incident_id: "inc_01NEW",
  type: "incident.opened",
  ref_id: "inc_01NEW",
  at: "2026-10-29T09:00:00Z",
};

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("a new incident shows a toast and counts on the bell", async () => {
  mockApi({ "GET /api/v1/incidents/inc_01NEW": INCIDENT });
  const { container } = renderWithQuery(<LiveIncidents />);
  expect(FakeEventSource.last.url).toBe("/api/v1/stream");
  act(() => FakeEventSource.last.emit("incident.opened", OPENED));
  const toast = await screen.findByRole("link", { name: INCIDENT.title });
  expect(toast.getAttribute("href")).toBe("/incidents/inc_01NEW");
  expect(screen.getByText("Critical")).toBeDefined();
  expect(container.querySelector("img")).toBeNull();
  const bell = screen.getByRole("link", { name: "1 new incident" });
  fireEvent.click(bell);
  expect(screen.getByRole("link", { name: "No new incidents" })).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByRole("link", { name: INCIDENT.title })).toBeNull();
});

test("other changes refresh data without a toast", async () => {
  const calls = mockApi({});
  renderWithQuery(<LiveIncidents />);
  act(() =>
    FakeEventSource.last.emit("incident.transition", {
      ...OPENED,
      type: "incident.transition",
    }),
  );
  await new Promise((r) => setTimeout(r, 20));
  expect(calls).toEqual([]);
  expect(screen.getByRole("link", { name: "No new incidents" })).toBeDefined();
});

test("a hidden tab gets a desktop notification once allowed", async () => {
  mockApi({ "GET /api/v1/incidents/inc_01NEW": INCIDENT });
  const shown = vi.fn();
  class FakeNotification {
    static permission = "default";
    static requestPermission = vi.fn(async () => {
      FakeNotification.permission = "granted";
      return "granted";
    });
    onclick: (() => void) | null = null;
    constructor(title: string, options: NotificationOptions) {
      shown(title, options);
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
  vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  renderWithQuery(<LiveIncidents />);
  fireEvent.click(
    screen.getByRole("button", { name: "Enable desktop alerts" }),
  );
  await vi.waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "Enable desktop alerts" }),
    ).toBeNull(),
  );
  act(() => FakeEventSource.last.emit("incident.opened", OPENED));
  await vi.waitFor(() => expect(shown).toHaveBeenCalled());
  expect(shown).toHaveBeenCalledWith(`Critical: ${INCIDENT.title}`, {
    body: "checkout · shop",
    tag: "inc_01NEW",
  });
});

test("a stream that cannot come back says so", async () => {
  renderWithQuery(<LiveIncidents />);
  expect(screen.queryByRole("alert")).toBeNull();
  act(() => FakeEventSource.last.fail(FakeEventSource.CLOSED));
  expect((await screen.findByRole("alert")).textContent).toContain(
    "Live updates stopped",
  );
});

test("a dropped stream shows reconnecting only if it stays down", async () => {
  vi.useFakeTimers();
  try {
    renderWithQuery(<LiveIncidents />);
    act(() => FakeEventSource.last.fail(FakeEventSource.CONNECTING));
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.queryByRole("alert")).toBeNull(); // a routine reconnect is silent
    act(() => vi.advanceTimersByTime(10_000));
    expect(screen.getByRole("alert").textContent).toContain("Reconnecting");
    act(() => FakeEventSource.last.onopen?.());
    expect(screen.queryByRole("alert")).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

test("the stream is closed when the page goes away", () => {
  const { unmount } = renderWithQuery(<LiveIncidents />);
  unmount();
  expect(FakeEventSource.last.closed).toBe(true);
});
