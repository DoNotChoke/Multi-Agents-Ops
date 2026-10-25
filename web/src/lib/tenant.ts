export const TENANT_COOKIE = "opsteam_tenant";

export type Membership = { tenant_id: string; roles: string[] };

/** The remembered tenant if the user still belongs to it, else their first one. */
export function pickTenant(
  memberships: Membership[],
  remembered: string | undefined,
): Membership | null {
  return (
    memberships.find((m) => m.tenant_id === remembered) ??
    memberships[0] ??
    null
  );
}
