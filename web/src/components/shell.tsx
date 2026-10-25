"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "Overview" },
  { group: "Settings" },
  { href: "/integrations", label: "Integrations" },
  { href: "/services", label: "Services" },
  { group: "Coming later" },
  { label: "Incidents", soon: "P2" },
  { label: "AI usage", soon: "P4" },
  { label: "Approvals", soon: "P6" },
] as const;

export type Account = {
  name: string;
  email: string;
  tenant: string;
  role: string;
};

export function Logo({ size = 26 }: { size?: number }) {
  return (
    <span
      className="grid place-items-center rounded-[25%] bg-brand-700"
      style={{ width: size, height: size }}
    >
      <svg
        width={size * 0.55}
        height={size * 0.55}
        viewBox="0 0 24 24"
        fill="none"
        stroke="#fff"
        strokeWidth="2.3"
        aria-hidden
      >
        <path d="M4 14h4l2-6 4 10 2-4h4" />
      </svg>
    </span>
  );
}

export function Shell({
  account,
  children,
}: {
  account?: Account;
  children: React.ReactNode;
}) {
  const path = usePathname();
  return (
    <div className="grid min-h-screen grid-cols-[228px_1fr]">
      <aside className="flex flex-col border-r border-line bg-white">
        <div className="flex items-center gap-2.5 border-b border-line-2 px-[18px] py-4">
          <Logo />
          <span className="text-[15px] font-semibold tracking-tight">
            opsteam
          </span>
        </div>
        {account && (
          <div className="m-3 rounded-md border border-line px-2.5 py-2">
            <strong>{account.tenant}</strong>
            <small className="block text-[11.5px] text-muted">
              {account.role}
            </small>
          </div>
        )}
        <nav aria-label="Main" className="flex flex-col gap-px px-2 py-1">
          {NAV.map((item, i) => {
            if ("group" in item) {
              return (
                <div
                  key={i}
                  className="px-2.5 pt-3.5 pb-1 text-[11px] tracking-wider text-muted uppercase"
                >
                  {item.group}
                </div>
              );
            }
            if ("soon" in item) {
              return (
                <span
                  key={i}
                  aria-disabled="true"
                  className="flex items-center px-2.5 py-[7px] text-[#9aa7b4]"
                >
                  {item.label}
                  <span className="ml-auto rounded-sm border border-line px-1 text-[10.5px] text-muted">
                    {item.soon}
                  </span>
                </span>
              );
            }
            const current =
              item.href === "/" ? path === "/" : path.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={current ? "page" : undefined}
                className="rounded-[5px] px-2.5 py-[7px] text-ink-2 hover:bg-wash aria-[current=page]:bg-brand-50 aria-[current=page]:font-semibold aria-[current=page]:text-brand-800 aria-[current=page]:shadow-[inset_2px_0_0_var(--color-brand-700)]"
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        {account && (
          <div className="mt-auto flex items-center gap-2.5 border-t border-line-2 px-3.5 py-3">
            <span className="grid size-7 place-items-center rounded-full bg-brand-100 text-xs font-semibold text-brand-800">
              {account.name.slice(0, 2).toUpperCase()}
            </span>
            <div className="leading-tight">
              <div className="font-semibold">{account.name}</div>
              <div className="text-xs text-muted">{account.email}</div>
            </div>
          </div>
        )}
      </aside>
      <main className="min-w-0 px-7 pt-[22px] pb-10">{children}</main>
    </div>
  );
}
