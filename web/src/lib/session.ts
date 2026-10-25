import { encode, getToken } from "next-auth/jwt";

const MAX_AGE = 30 * 24 * 60 * 60; // Auth.js default session lifetime
const COOKIE_LIMIT = 3900;

export type Session = { accessToken: string; cookie?: string };

function cookieName(secure: boolean): string {
  return secure ? "__Secure-authjs.session-token" : "authjs.session-token";
}

/** The session's Keycloak access token, refreshed when it is about to expire; null means sign in again. */
export async function freshToken(
  req: { headers: Headers },
  secure: boolean,
): Promise<Session | null> {
  const name = cookieName(secure);
  const secret = process.env.AUTH_SECRET ?? "";
  const token = await getToken({
    req,
    secret,
    secureCookie: secure,
    salt: name,
  });
  if (!token?.access_token) return null;
  if ((token.expires_at ?? 0) * 1000 > Date.now() + 30_000)
    return { accessToken: token.access_token };
  const refreshed = await refresh(token.refresh_token);
  if (!refreshed) return null;
  const value = await encode({
    token: { ...token, ...refreshed },
    secret,
    salt: name,
    maxAge: MAX_AGE,
  });
  // shortcut: writes one cookie; Auth.js would chunk a session over 4 KB, which Keycloak tokens stay under.
  if (value.length > COOKIE_LIMIT) return null;
  const flags = `Path=/; HttpOnly; SameSite=Lax; Max-Age=${MAX_AGE}${secure ? "; Secure" : ""}`;
  return {
    accessToken: refreshed.access_token,
    cookie: `${name}=${value}; ${flags}`,
  };
}

async function refresh(refreshToken: string | undefined) {
  if (!refreshToken) return null;
  const response = await fetch(
    `${process.env.AUTH_KEYCLOAK_ISSUER}/protocol/openid-connect/token`,
    {
      method: "POST",
      body: new URLSearchParams({
        grant_type: "refresh_token",
        refresh_token: refreshToken,
        client_id: process.env.AUTH_KEYCLOAK_ID ?? "",
        client_secret: process.env.AUTH_KEYCLOAK_SECRET ?? "",
      }),
      cache: "no-store",
    },
  );
  if (!response.ok) return null;
  const body = (await response.json()) as {
    access_token: string;
    refresh_token?: string;
    expires_in: number;
  };
  return {
    access_token: body.access_token,
    refresh_token: body.refresh_token ?? refreshToken,
    expires_at: Math.floor(Date.now() / 1000) + body.expires_in,
  };
}
