// @vitest-environment node
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const jwt = vi.hoisted(() => ({ getToken: vi.fn(), encode: vi.fn() }));
vi.mock("next-auth/jwt", () => jwt);
import { freshToken } from "./session";

const req = { headers: new Headers() };
const now = () => Math.floor(Date.now() / 1000);

beforeEach(() => {
  process.env.AUTH_KEYCLOAK_ISSUER = "http://kc.test/realms/opsteam";
  jwt.encode.mockResolvedValue("encrypted-session");
});
afterEach(() => vi.unstubAllGlobals());

test("a token that is still valid is used as is", async () => {
  jwt.getToken.mockResolvedValue({
    access_token: "dev-only-at",
    expires_at: now() + 300,
  });
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  expect(await freshToken(req, false)).toEqual({ accessToken: "dev-only-at" });
  expect(fetch).not.toHaveBeenCalled();
});

test("an expiring token is refreshed and the session cookie rewritten", async () => {
  jwt.getToken.mockResolvedValue({
    access_token: "old",
    refresh_token: "dev-only-rt",
    expires_at: now() + 5,
  });
  const fetch = vi
    .fn()
    .mockResolvedValue(
      Response.json({ access_token: "dev-only-new", expires_in: 300 }),
    );
  vi.stubGlobal("fetch", fetch);
  const session = await freshToken(req, true);
  expect(fetch.mock.calls[0][0]).toBe(
    "http://kc.test/realms/opsteam/protocol/openid-connect/token",
  );
  expect(String(fetch.mock.calls[0][1].body)).toContain(
    "grant_type=refresh_token",
  );
  expect(session?.accessToken).toBe("dev-only-new");
  expect(session?.cookie).toMatch(
    /^__Secure-authjs\.session-token=encrypted-session; .*HttpOnly; SameSite=Lax.*; Secure$/,
  );
  expect(jwt.encode.mock.calls[0][0].token).toMatchObject({
    access_token: "dev-only-new",
    refresh_token: "dev-only-rt",
  });
});

test("no session, or a refused refresh, means signing in again", async () => {
  jwt.getToken.mockResolvedValue(null);
  expect(await freshToken(req, false)).toBeNull();
  jwt.getToken.mockResolvedValue({
    access_token: "old",
    refresh_token: "dev-only-rt",
    expires_at: now() - 1,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("", { status: 400 })),
  );
  expect(await freshToken(req, false)).toBeNull();
});
