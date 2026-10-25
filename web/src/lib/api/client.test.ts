import { expect, test } from "vitest";
import { ApiError, unwrap } from "./client";

const response = (status: number, headers: Record<string, string> = {}) =>
  new Response(null, { status, headers });

test("unwrap returns data", () => {
  expect(unwrap({ data: { items: [] }, response: response(200) })).toEqual({
    items: [],
  });
});

test("unwrap throws the problem code and request id", () => {
  const error = { code: "service.exists", request_id: "01ABC" };
  expect(() => unwrap({ error, response: response(409) })).toThrow(
    expect.objectContaining({
      status: 409,
      code: "service.exists",
      requestId: "01ABC",
    }),
  );
});

test("a body that is not a problem document still yields a typed error", () => {
  try {
    unwrap({
      error: "<html>bad gateway</html>",
      response: response(502, { "x-request-id": "01XYZ" }),
    });
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError);
    expect(e).toMatchObject({ code: "http.502", requestId: "01XYZ" });
  }
});
