"use client";

import { useEffect, useId } from "react";

/** A modal panel over a dimmed page; Escape or the backdrop closes it. */
export function Dialog({
  title,
  onClose,
  side = false,
  children,
}: {
  title: string;
  onClose: () => void;
  side?: boolean;
  children: React.ReactNode;
}) {
  const id = useId();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40">
      <div className="absolute inset-0 bg-ink/30" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
        className={
          side
            ? "absolute inset-y-0 right-0 flex w-[440px] flex-col border-l border-line bg-white shadow-xl"
            : "absolute top-10 left-1/2 w-[540px] -translate-x-1/2 rounded-[10px] border border-line bg-white shadow-xl"
        }
      >
        <div className="flex items-center justify-between border-b border-line-2 px-[18px] py-3.5">
          <h2 id={id} className="text-[15px] font-semibold">
            {title}
          </h2>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="px-2 text-muted hover:text-ink"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="mb-3.5 grid gap-1.5">
      <span className="text-[13px] font-medium">{label}</span>
      {children}
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </label>
  );
}

export function FormError({
  error,
  message,
}: {
  error: unknown;
  message: string;
}) {
  const detail = error as { code?: string; requestId?: string | null };
  return (
    <div
      role="alert"
      className="rounded-md border border-[#efc9c6] bg-crit-bg px-3 py-2 text-[13px]"
    >
      {message}
      {detail?.code && (
        <span className="ml-1 font-mono text-xs text-muted">
          ({detail.code}
          {detail.requestId && ` · ${detail.requestId}`})
        </span>
      )}
    </div>
  );
}
