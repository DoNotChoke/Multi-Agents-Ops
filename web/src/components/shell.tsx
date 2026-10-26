"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/incidents", label: "Incidents" },
  { group: "Settings" },
  { href: "/integrations", label: "Integrations" },
  { href: "/services", label: "Services" },
  { group: "Coming later" },
  { label: "AI usage", soon: "P4" },
  { label: "Approvals", soon: "P6" },
] as const;

export type Account = {
  name: string;
  email: string;
  tenant: string;
  tenantId: string;
  role: string;
};

type Props = {
  account?: Account;
  tenants?: { id: string; name: string }[];
  onSwitchTenant?: (form: FormData) => Promise<void>;
  onSignOut?: () => Promise<void>;
  children: React.ReactNode;
};

// Sizes are classes, not style attributes: the CSP allows no inline styles.
const LOGO = {
  sm: "size-[26px] rounded-md [&>svg]:size-[14px]",
  lg: "size-14 rounded-[14px] [&>svg]:size-[30px]",
};

export function Logo({ size = "sm" }: { size?: keyof typeof LOGO }) {
  return (
    <span className={`grid place-items-center bg-brand-700 ${LOGO[size]}`}>
      <svg
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
  tenants = [],
  onSwitchTenant,
  onSignOut,
  children,
}: Props) {
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
            {tenants.length > 1 && onSwitchTenant ? (
              <form action={onSwitchTenant}>
                <select
                  name="tenant"
                  aria-label="Organization"
                  defaultValue={account.tenantId}
                  onChange={(e) => e.currentTarget.form?.requestSubmit()}
                  className="w-full bg-transparent font-semibold"
                >
                  {tenants.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </form>
            ) : (
              <strong>{account.tenant}</strong>
            )}
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
              {account.name
                .split(/\s+/)
                .map((word) => word[0])
                .join("")
                .slice(0, 2)
                .toUpperCase()}
            </span>
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate font-semibold">{account.name}</div>
              <div className="truncate text-xs text-muted">{account.email}</div>
            </div>
            {onSignOut && (
              <form action={onSignOut}>
                <button
                  type="submit"
                  className="text-xs text-muted hover:text-ink"
                >
                  Sign out
                </button>
              </form>
            )}
          </div>
        )}
      </aside>
      <main className="min-w-0 px-7 pt-[22px] pb-10">{children}</main>
    </div>
  );
}
