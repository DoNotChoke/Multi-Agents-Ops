import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { vi } from "vitest";

type Reply = unknown | ((request: Request, body: unknown) => unknown);

/** Answers fetches from a "METHOD /path" table; a Response is returned as is, anything else as JSON. */
export function mockApi(routes: Record<string, Reply>) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetch = vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    const text = request.method === "GET" ? "" : await request.text();
    const body = text ? JSON.parse(text) : undefined;
    calls.push({ method: request.method, path, body });
    const reply = routes[`${request.method} ${path}`];
    if (reply === undefined) return new Response(null, { status: 404 });
    const value = typeof reply === "function" ? reply(request, body) : reply;
    return value instanceof Response ? value : Response.json(value);
  });
  vi.stubGlobal("fetch", fetch);
  return calls;
}

export function problem(status: number, code: string, requestId = "01REQ") {
  return Response.json({ status, code, request_id: requestId }, { status });
}

export function renderWithQuery(ui: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}
