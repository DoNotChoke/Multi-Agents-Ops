import createClient from "openapi-fetch";
import type { paths } from "@/generated/api";

// Same-origin: the Next.js server forwards /api/v1/* to the API with the session's token.
export const api = createClient<paths>({
  // The page's own origin; an absolute base also works where Request rejects relative URLs.
  baseUrl: globalThis.location?.origin ?? "",
  fetch: (request) => globalThis.fetch(request),
});

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

const MESSAGES: Record<string, string> = {
  "netguard.blocked":
    "Private and cloud metadata addresses are blocked. Use a public address.",
  "integration.exists": "An integration with this name already exists.",
  "service.exists": "A service with this name and environment already exists.",
  "integration.unavailable": "The integration could not be reached.",
  "request.invalid": "Some fields are not valid.",
  "authz.forbidden": "You don't have permission to do this.",
  "auth.session_expired": "Your session has ended. Sign in again.",
};

/** A sentence for the user; unknown codes fall back to a generic line (the code is shown beside it). */
export function errorMessage(error: unknown): string {
  return (
    (error instanceof ApiError && MESSAGES[error.code]) ||
    "Something went wrong."
  );
}
