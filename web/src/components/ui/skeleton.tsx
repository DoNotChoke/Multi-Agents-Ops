export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={`h-3 animate-pulse rounded bg-line-2 ${className}`}
    />
  );
}
