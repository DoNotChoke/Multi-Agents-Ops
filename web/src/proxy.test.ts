// @vitest-environment node
import { NextRequest } from "next/server";
import { expect, test } from "vitest";
import { proxy } from "./proxy";

test("pages carry a nonce CSP and the security headers", () => {
  const response = proxy(new NextRequest("http://localhost:3000/integrations"));
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
    proxy(new NextRequest("http://localhost:3000/"))
      .headers.get("content-security-policy")
      ?.match(/nonce-([^']+)/)?.[1];
  expect(nonce()).not.toBe(nonce());
});
