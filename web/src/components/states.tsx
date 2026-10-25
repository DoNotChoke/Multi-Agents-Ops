import { ApiError } from "@/lib/api/client";
import { Button } from "./ui/button";
import { Skeleton } from "./ui/skeleton";

export function Loading({ rows = 4 }: { rows?: number }) {
  return (
    <div role="status" aria-label="Loading" className="grid gap-3 p-4">
      <Skeleton className="w-2/5" />
      {Array.from({ length: rows - 1 }, (_, i) => (
        <Skeleton key={i} className={i % 2 ? "w-4/5" : "w-full"} />
      ))}
    </div>
  );
}

export function Empty({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="px-5 py-12 text-center">
      <div className="font-semibold">{title}</div>
      {hint && <p className="mt-1 text-muted">{hint}</p>}
      {action && <div className="mt-3.5">{action}</div>}
    </div>
  );
}

export function Forbidden() {
  return (
    <Empty
      title="You don't have access"
      hint="Ask an admin of this organization."
    />
  );
}

/** Shows the problem code and request id so a report can be traced; 403/404 read as no access. */
export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  if (
    error instanceof ApiError &&
    (error.status === 403 || error.status === 404)
  ) {
    return <Forbidden />;
  }
  const code = error instanceof ApiError ? error.code : "client.error";
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div
      role="alert"
      className="m-4 flex items-center gap-3 rounded-lg border border-[#efc9c6] bg-crit-bg px-3.5 py-3"
    >
      <div className="flex-1">
        <div className="font-semibold">Something went wrong</div>
        <div className="mt-1 font-mono text-xs text-muted">
          {code}
          {requestId && ` · ${requestId}`}
        </div>
      </div>
      {onRetry && <Button onClick={onRetry}>Retry</Button>}
    </div>
  );
}
