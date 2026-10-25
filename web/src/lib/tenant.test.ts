import { expect, test } from "vitest";
import { pickTenant } from "./tenant";

const a = { tenant_id: "tnt_a", roles: ["admin"] };
const b = { tenant_id: "tnt_b", roles: ["viewer"] };

test("keeps the remembered tenant, falls back to the first, or none", () => {
  expect(pickTenant([a, b], "tnt_b")).toBe(b);
  expect(pickTenant([a, b], "tnt_gone")).toBe(a);
  expect(pickTenant([a, b], undefined)).toBe(a);
  expect(pickTenant([], "tnt_a")).toBeNull();
});
