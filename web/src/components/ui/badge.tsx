const TONES = {
  ok: "bg-ok-bg text-ok",
  warn: "bg-warn-bg text-warn",
  crit: "bg-crit-bg text-crit",
  info: "bg-brand-50 text-brand-700",
  off: "bg-line-2 text-muted",
} as const;

export type Tone = keyof typeof TONES;

export function Badge({
  tone,
  children,
}: {
  tone: Tone;
  children: React.ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 text-xs font-medium whitespace-nowrap before:size-1.5 before:rounded-full before:bg-current ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}
