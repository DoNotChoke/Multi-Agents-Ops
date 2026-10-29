import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { connection } from "next/server";
import { auth, signOut } from "@/auth";
import { Shell } from "@/components/shell";
import { Empty } from "@/components/states";
import { LiveIncidents } from "@/features/incidents/live";
import { apiGet } from "@/lib/bff";
import { freshToken } from "@/lib/session";
import { pickTenant, TENANT_COOKIE, type Membership } from "@/lib/tenant";
import { Providers } from "../providers";

// Every page reads the session, and CSP nonces need a per-request render, so nothing prerenders.
export const instant = false;

async function switchTenant(form: FormData) {
  "use server";
  const secure = (await headers()).get("x-forwarded-proto") === "https";
  // The API checks membership on every call, so a forged value only yields 403s.
  (await cookies()).set(TENANT_COOKIE, String(form.get("tenant")), {
    httpOnly: true,
    sameSite: "lax",
    secure,
    path: "/",
  });
  redirect("/");
}

async function leave() {
  "use server";
  // shortcut: ends only our session, not Keycloak's SSO session; add RP-initiated logout with id_token_hint when needed.
  await signOut({ redirectTo: "/sign-in" });
}

export default async function AppLayout({ children }: LayoutProps<"/">) {
  await connection(); // auth() draws random values; never prerender
  const session = await auth();
  const requestHeaders = await headers();
  const token =
    session &&
    (await freshToken(
      { headers: requestHeaders },
      requestHeaders.get("x-forwarded-proto") === "https",
    ));
  // proxy.ts sends visitors without a session cookie to /sign-in; this covers an expired one.
  if (!session?.user || !token) {
    return (
      <Empty
        title="Your session has ended"
        action={
          // Our cookie outlives the Keycloak tokens inside it; clear it first or /sign-in sends us straight back.
          <form action={leave}>
            <button type="submit" className="text-brand-700 hover:underline">
              Sign in again
            </button>
          </form>
        }
      />
    );
  }

  const me = await apiGet<{ memberships: Membership[] }>(
    "/api/v1/me",
    token.accessToken,
  );
  const active = pickTenant(
    me.memberships,
    (await cookies()).get(TENANT_COOKIE)?.value,
  );
  const tenants = await Promise.all(
    me.memberships.map(async (m) => ({
      id: m.tenant_id,
      name: (
        await apiGet<{ name: string }>(
          "/api/v1/tenant",
          token.accessToken,
          m.tenant_id,
        )
      ).name,
    })),
  );
  const account = active && {
    name: session.user.name ?? session.user.email ?? "",
    email: session.user.email ?? "",
    tenant:
      tenants.find((t) => t.id === active.tenant_id)?.name ?? active.tenant_id,
    tenantId: active.tenant_id,
    role: active.roles.map((r) => r[0].toUpperCase() + r.slice(1)).join(", "),
  };
  return (
    <Providers>
      <Shell
        account={account ?? undefined}
        tenants={tenants}
        onSwitchTenant={switchTenant}
        onSignOut={leave}
        live={account ? <LiveIncidents /> : undefined}
      >
        {account ? (
          children
        ) : (
          <Empty
            title="You are not a member of any organization yet"
            hint="Ask an admin to add you."
          />
        )}
      </Shell>
    </Providers>
  );
}
