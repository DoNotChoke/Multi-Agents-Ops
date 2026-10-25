// @vitest-environment node
import { NextRequest } from "next/server";
import { expect, test } from "vitest";
import { proxy } from "./proxy";

const signedIn = (url: string) =>
  new NextRequest(url, { headers: { cookie: "authjs.session-token=sealed" } });

test("pages carry a nonce CSP and the security headers", () => {
  const response = proxy(signedIn("http://localhost:3000/integrations"));
  const csp = response.headers.get("content-security-policy") ?? "";
  expect(csp).toMatch(
    /script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/,
  );
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).toContain("object-src 'none'");
  expect(response.headers.get("x-content-type-options")).toBe("nosniff");
  expect(response.headers.get("referrer-policy")).toBe(
    "strict-origin-when-cross-origin",
  );
  expect(response.headers.get("x-frame-options")).toBe("DENY");
});

test("every request gets a fresh nonce", () => {
  const nonce = () =>
    proxy(signedIn("http://localhost:3000/"))
      .headers.get("content-security-policy")
      ?.match(/nonce-([^']+)/)?.[1];
  const first = nonce();
  expect(first).toBeTruthy();
  expect(first).not.toBe(nonce());
});

test("a visitor without a session is sent to sign in, before anything renders", () => {
  const response = proxy(new NextRequest("http://localhost:3000/services"));
  expect(response.status).toBe(307);
  expect(response.headers.get("location")).toBe(
    "http://localhost:3000/sign-in",
  );
});

test("the sign-in and sign-up pages stay reachable without a session", () => {
  for (const path of ["/sign-in", "/sign-up"]) {
    const response = proxy(new NextRequest(`http://localhost:3000${path}`));
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("content-security-policy")).toContain(
      "frame-ancestors 'none'",
    );
  }
});
