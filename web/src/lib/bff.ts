import type { NextRequest } from "next/server";
import { freshToken } from "./session";
import { pickTenant, TENANT_COOKIE, type Membership } from "./tenant";

export const API_URL = process.env.OPSTEAM_API_URL ?? "http://127.0.0.1:8000";

function problem(status: number, code: string): Response {
  return Response.json(
    { type: "about:blank", status, code, request_id: null },
    {
      status,
      headers: {
        "content-type": "application/problem+json",
        "x-content-type-options": "nosniff",
      },
    },
  );
}

/** GET from the API with the user's token, server side. */
export async function apiGet<T>(
  path: string,
  accessToken: string,
  tenant?: string,
): Promise<T> {
  const headers: Record<string, string> = {
    authorization: `Bearer ${accessToken}`,
    accept: "application/json",
  };
  if (tenant) headers["x-tenant-id"] = tenant;
  const response = await fetch(new URL(path, API_URL), {
    headers,
    cache: "no-store",
  });
  if (!response.ok)
    throw new Error(`GET ${path} failed with ${response.status}`);
  return (await response.json()) as T;
}

/** Forwards a same-origin /api/v1/* call to the API with the session's token and tenant. */
export async function forward(req: NextRequest): Promise<Response> {
  const safe = req.method === "GET" || req.method === "HEAD";
  // SameSite=Lax already keeps the cookie off cross-site writes; the Origin check covers same-site ones.
  if (!safe && req.headers.get("origin") !== req.nextUrl.origin)
    return problem(403, "csrf.origin_mismatch");
  const secure = req.nextUrl.protocol === "https:";
  const session = await freshToken(req, secure);
  if (!session) return problem(401, "auth.session_expired");

  const out = new Headers({
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
  });
  let tenant = req.cookies.get(TENANT_COOKIE)?.value;
  if (!tenant) {
    const me = await apiGet<{ memberships: Membership[] }>(
      "/api/v1/me",
      session.accessToken,
    );
    tenant = pickTenant(me.memberships, undefined)?.tenant_id;
    if (tenant)
      out.append(
        "set-cookie",
        `${TENANT_COOKIE}=${tenant}; Path=/; HttpOnly; SameSite=Lax${secure ? "; Secure" : ""}`,
      );
  }

  // Live updates stream as server-sent events; everything else is JSON.
  const stream = req.headers.get("accept") === "text/event-stream";
  const headers = new Headers({
    authorization: `Bearer ${session.accessToken}`,
    accept: stream ? "text/event-stream" : "application/json",
  });
  if (tenant) headers.set("x-tenant-id", tenant);
  const resume = req.headers.get("last-event-id");
  if (stream && resume) headers.set("last-event-id", resume);
  const type = req.headers.get("content-type");
  if (type) headers.set("content-type", type);
  const upstream = await fetch(
    new URL(req.nextUrl.pathname + req.nextUrl.search, API_URL),
    {
      method: req.method,
      headers,
      body: safe ? undefined : await req.arrayBuffer(),
      redirect: "manual",
      cache: "no-store",
      // Closing the tab ends the upstream stream too.
      signal: req.signal,
    },
  );
  for (const name of ["content-type", "x-request-id", "x-accel-buffering"]) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  if (session.cookie) out.append("set-cookie", session.cookie);
  return new Response(upstream.body, { status: upstream.status, headers: out });
}
