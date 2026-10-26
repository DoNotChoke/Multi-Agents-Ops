"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, PageHeader } from "@/components/ui/card";
import type { components, paths } from "@/generated/api";
import { api, unwrap } from "@/lib/api/client";

type Summary = components["schemas"]["IncidentSummary"];
type Query = NonNullable<
  paths["/api/v1/incidents"]["get"]["parameters"]["query"]
>;

export const OPEN = [
  "NEW",
  "TRIAGED",
  "INVESTIGATING",
  "DIAGNOSED",
  "INCONCLUSIVE",
  "REMEDIATION_PROPOSED",
  "AWAITING_APPROVAL",
  "EXECUTING",
  "VERIFYING",
  "MANUAL_ACTION_REQUIRED",
] as const;
const VIEWS = {
  open: OPEN,
  resolved: ["RESOLVED"],
  closed: ["CLOSED"],
  all: undefined,
} as const;
const SEVERITY_TONE: Record<string, Tone> = {
  critical: "crit",
  high: "crit",
  medium: "warn",
  low: "off",
  info: "info",
};

const titleCase = (s: string) =>
  s.charAt(0) + s.slice(1).toLowerCase().replaceAll("_", " ");

export function SeverityBadge({ severity }: { severity: string }) {
  return (
    <Badge tone={SEVERITY_TONE[severity] ?? "off"}>
      {severity.charAt(0).toUpperCase() + severity.slice(1)}
    </Badge>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const tone: Tone =
    status === "RESOLVED" ? "ok" : status === "CLOSED" ? "off" : "info";
  return <Badge tone={tone}>{titleCase(status)}</Badge>;
}

/** UTC to the minute or second, so every viewer reads the same clock. */
export function utc(at: string, seconds = false) {
  return `${new Date(at)
    .toISOString()
    .slice(0, seconds ? 19 : 16)
    .replace("T", " ")} UTC`;
}

/** The catalog service, or the raw labels with a hint that it is not in the catalog. */
export function ServiceCell({ incident }: { incident: Summary }) {
  if (incident.service) {
    return (
      <span>
        {incident.service.name}{" "}
        <span className="rounded border border-line bg-wash px-1.5 font-mono text-xs">
          {incident.service.environment}
        </span>
      </span>
    );
  }
  const raw = incident.unmapped_service;
  return (
    <span className="flex items-center gap-2">
      {raw?.name && <span>{raw.name}</span>}
      <Badge tone="off">Unmapped</Badge>
    </span>
  );
}

export function IncidentsScreen() {
  const [view, setView] = useState<keyof typeof VIEWS>("open");
  const [severity, setSeverity] = useState("");
  const query: Query = {
    limit: 50,
    ...(VIEWS[view] && { status: [...VIEWS[view]] }),
    ...(severity && { severity: [severity as "critical"] }),
  };
  const incidents = useInfiniteQuery({
    queryKey: ["incidents", view, severity],
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/v1/incidents", {
          params: {
            query: { ...query, ...(pageParam && { cursor: pageParam }) },
          },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const items = incidents.data?.pages.flatMap((p) => p.items) ?? [];

  const select =
    "h-8 rounded-md border border-[#cfd7e0] bg-white px-2 text-[13px]";
  const filters = (
    <div className="flex gap-2">
      <label className="sr-only" htmlFor="incident-status">
        Status
      </label>
      <select
        id="incident-status"
        value={view}
        onChange={(e) => setView(e.target.value as keyof typeof VIEWS)}
        className={select}
      >
        <option value="open">Open</option>
        <option value="resolved">Resolved</option>
        <option value="closed">Closed</option>
        <option value="all">All</option>
      </select>
      <label className="sr-only" htmlFor="incident-severity">
        Severity
      </label>
      <select
        id="incident-severity"
        value={severity}
        onChange={(e) => setSeverity(e.target.value)}
        className={select}
      >
        <option value="">Any severity</option>
        {["critical", "high", "medium", "low", "info"].map((s) => (
          <option key={s} value={s}>
            {s.charAt(0).toUpperCase() + s.slice(1)}
          </option>
        ))}
      </select>
    </div>
  );

  return (
    <>
      <PageHeader
        title="Incidents"
        subtitle="Opened automatically from your alerts"
        actions={filters}
      />
      <Card>
        {incidents.isPending ? (
          <Loading />
        ) : incidents.isError ? (
          <ErrorState
            error={incidents.error}
            onRetry={() => incidents.refetch()}
          />
        ) : items.length === 0 ? (
          <Empty
            title="No incidents"
            hint="Incidents open here when your Alertmanager sends alerts."
          />
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-line bg-[#fbfcfd] text-left text-xs text-muted">
                <th className="px-4 py-2.5 font-medium">Severity</th>
                <th className="px-4 py-2.5 font-medium">Incident</th>
                <th className="px-4 py-2.5 font-medium">Service</th>
                <th className="px-4 py-2.5 font-medium">Status</th>
                <th className="px-4 py-2.5 font-medium">Opened</th>
                <th className="px-4 py-2.5 text-right font-medium">Alerts</th>
              </tr>
            </thead>
            <tbody>
              {items.map((i) => (
                <tr
                  key={i.incident_id}
                  className="border-b border-line-2 last:border-0"
                >
                  <td className="px-4 py-2.5">
                    <SeverityBadge severity={i.severity} />
                  </td>
                  {/* Titles come from alert annotations: plain text inside our own link. */}
                  <td className="px-4 py-2.5 font-semibold">
                    <Link
                      href={`/incidents/${i.incident_id}`}
                      className="hover:text-brand-700 hover:underline"
                    >
                      {i.title}
                    </Link>
                  </td>
                  <td className="px-4 py-2.5">
                    <ServiceCell incident={i} />
                  </td>
                  <td className="px-4 py-2.5">
                    <StatusBadge status={i.status} />
                  </td>
                  <td className="px-4 py-2.5 font-mono text-xs text-muted">
                    {utc(i.opened_at)}
                  </td>
                  <td className="px-4 py-2.5 text-right">{i.event_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {incidents.hasNextPage && (
        <div className="mt-3 text-center">
          <Button
            onClick={() => incidents.fetchNextPage()}
            disabled={incidents.isFetchingNextPage}
          >
            {incidents.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
    </>
  );
}
