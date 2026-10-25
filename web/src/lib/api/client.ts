import createClient from "openapi-fetch";
import type { paths } from "@/generated/api";

// Same-origin: the Next.js server forwards /api/v1/* to the API with the session's token.
export const api = createClient<paths>({ baseUrl: "" });

/** A failed API call, carrying the problem document's code and request id. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly requestId: string | null,
  ) {
    super(code);
  }
}

/** Return `data`, or throw an ApiError built from the RFC 7807 body. */
export function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): T {
  if (result.data !== undefined) return result.data;
  const body = (result.error ?? {}) as { code?: unknown; request_id?: unknown };
  throw new ApiError(
    result.response.status,
    typeof body.code === "string"
      ? body.code
      : `http.${result.response.status}`,
    typeof body.request_id === "string"
      ? body.request_id
      : result.response.headers.get("x-request-id"),
  );
}
