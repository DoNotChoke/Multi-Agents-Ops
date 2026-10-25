// @vitest-environment node
import { NextRequest } from "next/server";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const session = vi.hoisted(() => ({ freshToken: vi.fn() }));
vi.mock("./session", () => session);
import { forward } from "./bff";

const fetch = vi.fn();
beforeEach(() => {
  vi.stubGlobal("fetch", fetch);
  session.freshToken.mockResolvedValue({ accessToken: "dev-only-at" });
  fetch.mockResolvedValue(
    new Response('{"items":[]}', {
      headers: {
        "content-type": "application/json",
        "x-request-id": "01R",
        "set-cookie": "upstream=1",
      },
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  fetch.mockReset();
});

const request = (
  path: string,
  init: ConstructorParameters<typeof NextRequest>[1] = {},
) => new NextRequest(`http://localhost:3000${path}`, init);

test("forwards with the session token and tenant; the browser never sends either", async () => {
  const response = await forward(
    request("/api/v1/services?limit=5", {
      headers: {
        cookie: "opsteam_tenant=tnt_a",
        authorization: "Bearer forged",
      },
    }),
  );
  const [url, init] = fetch.mock.calls[0];
  expect(String(url)).toBe("http://127.0.0.1:8000/api/v1/services?limit=5");
  expect(init.headers.get("authorization")).toBe("Bearer dev-only-at");
  expect(init.headers.get("x-tenant-id")).toBe("tnt_a");
  expect(response.status).toBe(200);
  expect(response.headers.get("x-request-id")).toBe("01R");
  expect(response.headers.get("set-cookie")).toBeNull(); // upstream cookies are not relayed
});

test("a write from another origin is refused before anything is forwarded", async () => {
  const response = await forward(
    request("/api/v1/services", {
      method: "POST",
      headers: { origin: "https://evil.test" },
      body: "{}",
    }),
  );
  expect(response.status).toBe(403);
  expect((await response.json()).code).toBe("csrf.origin_mismatch");
  expect(fetch).not.toHaveBeenCalled();
});

test("a same-origin write passes its body through", async () => {
  await forward(
    request("/api/v1/services", {
      method: "POST",
      headers: {
        origin: "http://localhost:3000",
        cookie: "opsteam_tenant=tnt_a",
        "content-type": "application/json",
      },
      body: '{"name":"checkout"}',
    }),
  );
  const init = fetch.mock.calls[0][1];
  expect(new TextDecoder().decode(init.body)).toBe('{"name":"checkout"}');
  expect(init.headers.get("content-type")).toBe("application/json");
});

test("no session is a 401 problem", async () => {
  session.freshToken.mockResolvedValue(null);
  const response = await forward(request("/api/v1/services"));
  expect(response.status).toBe(401);
  expect(response.headers.get("content-type")).toBe("application/problem+json");
});

test("without a tenant cookie the first membership is used and remembered", async () => {
  fetch.mockResolvedValueOnce(
    Response.json({
      memberships: [{ tenant_id: "tnt_first", roles: ["admin"] }],
    }),
  );
  session.freshToken.mockResolvedValue({
    accessToken: "dev-only-at",
    cookie: "authjs.session-token=new",
  });
  const response = await forward(request("/api/v1/services"));
  expect(fetch.mock.calls[1][1].headers.get("x-tenant-id")).toBe("tnt_first");
  const cookies = response.headers.getSetCookie();
  expect(
    cookies.some(
      (c) =>
        c.startsWith("opsteam_tenant=tnt_first;") && c.includes("HttpOnly"),
    ),
  ).toBe(true);
  expect(cookies).toContain("authjs.session-token=new");
});
