import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "@/lib/api/client";
import { ErrorState } from "./states";

afterEach(cleanup);

test("error state shows the problem code and request id, and retries", () => {
  const retry = vi.fn();
  render(
    <ErrorState
      error={new ApiError(502, "integration.unavailable", "01M5S48K9DM7")}
      onRetry={retry}
    />,
  );
  expect(screen.getByRole("alert").textContent).toContain(
    "integration.unavailable · 01M5S48K9DM7",
  );
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(retry).toHaveBeenCalledOnce();
});

test.each([403, 404])(
  "a %i reads as no access, without saying whether the thing exists",
  (status) => {
    render(
      <ErrorState error={new ApiError(status, "authz.forbidden", "r1")} />,
    );
    expect(screen.getByText("You don't have access")).toBeDefined();
    expect(screen.queryByRole("alert")).toBeNull();
  },
);
