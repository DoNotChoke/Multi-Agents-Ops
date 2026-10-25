import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Shell } from "./shell";

vi.mock("next/navigation", () => ({
  usePathname: () => "/integrations/int_1",
}));
afterEach(cleanup);

test("the section of the current page is marked current", () => {
  render(<Shell>page</Shell>);
  expect(
    screen
      .getByRole("link", { name: "Integrations" })
      .getAttribute("aria-current"),
  ).toBe("page");
  expect(
    screen.getByRole("link", { name: "Overview" }).getAttribute("aria-current"),
  ).toBeNull();
});

test("account details appear only when known", () => {
  render(
    <Shell
      account={{
        name: "Dev Admin",
        email: "a@x.test",
        tenant: "acme-dev",
        role: "Admin",
      }}
    >
      p
    </Shell>,
  );
  expect(screen.getByText("acme-dev")).toBeDefined();
  expect(screen.getByText("a@x.test")).toBeDefined();
});
