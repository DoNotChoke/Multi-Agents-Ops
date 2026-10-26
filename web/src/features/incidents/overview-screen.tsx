"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Card, PageHeader } from "@/components/ui/card";
import { api, unwrap } from "@/lib/api/client";
import { OPEN, SeverityBadge, StatusBadge, utc } from "./incidents-screen";

const ORDER = ["critical", "high", "medium", "low", "info"];

function Stat({
  label,
  value,
  detail,
}: {
  label: string;
  value: React.ReactNode;
  detail?: string;
}) {
  return (
    <Card>
      <section aria-label={label} className="px-4 py-3.5">
        <div className="text-xs text-muted">{label}</div>
        <div className="mt-1 text-2xl font-semibold tracking-tight">
          {value}
        </div>
        {detail && <div className="mt-0.5 text-xs text-muted">{detail}</div>}
      </section>
    </Card>
  );
}

export function OverviewScreen() {
  // shortcut: counts come from the latest 200 incidents; add a summary endpoint when that is not enough.
  const incidents = useQuery({
    queryKey: ["incidents", "overview"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/incidents", {
          params: { query: { limit: 200 } },
        }),
      ).items,
  });
  const services = useQuery({
    queryKey: ["services"],
    queryFn: async () => unwrap(await api.GET("/api/v1/services")).items,
  });
  const integrations = useQuery({
    queryKey: ["integrations"],
    queryFn: async () => unwrap(await api.GET("/api/v1/integrations")).items,
  });

  if (incidents.isError)
    return (
      <>
        <PageHeader title="Overview" />
        <Card>
          <ErrorState
            error={incidents.error}
            onRetry={() => incidents.refetch()}
          />
        </Card>
      </>
    );

  const all = incidents.data ?? [];
  const open = all.filter((i) =>
    (OPEN as readonly string[]).includes(i.status),
  );
  const bySeverity = ORDER.map(
    (s) => [s, open.filter((i) => i.severity === s).length] as const,
  )
    .filter(([, n]) => n > 0)
    .map(([s, n]) => `${n} ${s}`)
    .join(" · ");
  const active = (integrations.data ?? []).filter(
    (i) => i.status === "active",
  ).length;

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="Incidents and coverage at a glance"
      />
      <div className="mb-4 grid grid-cols-3 gap-4">
        <Stat
          label="Open incidents"
          value={incidents.isPending ? "…" : open.length}
          detail={bySeverity || "All clear"}
        />
        <Stat
          label="Services"
          value={services.data?.length ?? "…"}
          detail="in the catalog"
        />
        <Stat
          label="Integrations"
          value={integrations.data?.length ?? "…"}
          detail={
            integrations.data
              ? `${active} of ${integrations.data.length} active`
              : undefined
          }
        />
      </div>
      <Card>
        <section aria-label="Recent incidents">
          <div className="flex items-center justify-between border-b border-line-2 px-4 py-3">
            <h2 className="text-[15px] font-semibold">Recent incidents</h2>
            <Link
              href="/incidents"
              className="text-[13px] text-brand-700 hover:underline"
            >
              View all
            </Link>
          </div>
          {incidents.isPending ? (
            <Loading />
          ) : all.length === 0 ? (
            <Empty
              title="No incidents yet"
              hint="They open here when your Alertmanager sends alerts."
            />
          ) : (
            <ul>
              {all.slice(0, 5).map((i) => (
                <li
                  key={i.incident_id}
                  className="flex items-center gap-3 border-b border-line-2 px-4 py-2.5 last:border-0"
                >
                  <SeverityBadge severity={i.severity} />
                  <Link
                    href={`/incidents/${i.incident_id}`}
                    className="min-w-0 flex-1 truncate font-medium hover:text-brand-700 hover:underline"
                  >
                    {i.title}
                  </Link>
                  <StatusBadge status={i.status} />
                  <span className="font-mono text-xs text-muted">
                    {utc(i.opened_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </Card>
    </>
  );
}
