"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import type { components } from "@/generated/api";
import { MapServiceDialog } from "@/features/catalog/mappings";
import { api, unwrap } from "@/lib/api/client";
import { SeverityBadge, StatusBadge, utc } from "./incidents-screen";

type Incident = components["schemas"]["Incident"];
type Alert = Incident["events"][number];

/** Only http(s) links from alert data become clickable; anything else stays text. */
function safeHref(value: string | undefined) {
  return value && /^https?:\/\//i.test(value) ? value : null;
}

export function IncidentScreen({ incidentId }: { incidentId: string }) {
  const [mapping, setMapping] = useState(false);
  const incident = useQuery({
    queryKey: ["incident", incidentId],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/incidents/{incident_id}", {
          params: { path: { incident_id: incidentId } },
        }),
      ),
  });

  if (incident.isPending)
    return (
      <Card>
        <Loading />
      </Card>
    );
  if (incident.isError)
    return (
      <Card>
        <ErrorState error={incident.error} onRetry={() => incident.refetch()} />
      </Card>
    );
  const i = incident.data;
  const service = i.service ?? i.unmapped_service;

  return (
    <>
      <div className="mb-2 text-[12.5px] text-muted">
        <Link href="/incidents" className="hover:underline">
          Incidents
        </Link>{" "}
        / <span className="font-mono">{i.incident_id}</span>
      </div>
      <div className="mb-[18px] flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <SeverityBadge severity={i.severity} />
            <h1 className="text-xl font-semibold tracking-tight">{i.title}</h1>
          </div>
          <p className="mt-1 text-[13px] text-muted">
            {service?.name && (
              <strong className="text-ink">{service.name}</strong>
            )}
            {service?.environment && ` · ${service.environment}`}
            {` · opened ${utc(i.opened_at, true)}`}
            {i.time_to_detect_seconds != null &&
              ` · detected in ${Math.round(i.time_to_detect_seconds)} s`}
          </p>
        </div>
        <StatusBadge status={i.status} />
      </div>

      {!i.service && i.unmapped_service?.name && (
        <div className="mb-4 rounded-md border border-warn/40 bg-warn-bg px-3 py-2 text-[13px]">
          {i.unmapped_service.name}
          {i.unmapped_service.environment &&
            ` in ${i.unmapped_service.environment}`}{" "}
          is not in the catalog, so this incident has no topology or
          criticality.{" "}
          <button
            type="button"
            onClick={() => setMapping(true)}
            className="font-medium text-brand-700 hover:underline"
          >
            Map to service…
          </button>
        </div>
      )}
      {mapping && (
        <MapServiceDialog
          incidentId={i.incident_id}
          labels={i.events[0]?.labels ?? {}}
          onClose={() => setMapping(false)}
        />
      )}

      <div className="grid grid-cols-[1fr_320px] items-start gap-4">
        <Card>
          <section aria-label="Alerts">
            <h2 className="border-b border-line-2 px-4 py-3 text-[15px] font-semibold">
              Alerts
            </h2>
            {i.events.length === 0 ? (
              <p className="px-4 py-3 text-muted">No alerts attached.</p>
            ) : (
              <ul>
                {i.events.map((e) => (
                  <AlertRow key={e.event_id} alert={e} />
                ))}
              </ul>
            )}
            {i.events_truncated && (
              <p className="px-4 py-2 text-xs text-muted">
                Showing the first 200 alerts.
              </p>
            )}
          </section>
        </Card>
        <Card>
          <section aria-label="Timeline">
            <h2 className="border-b border-line-2 px-4 py-3 text-[15px] font-semibold">
              Timeline
            </h2>
            <ol className="px-4 py-2">
              {i.timeline.map((t, n) => (
                <li
                  key={n}
                  className="border-b border-line-2 py-2 last:border-0"
                >
                  <div className="flex items-center justify-between gap-2">
                    <StatusBadge status={t.to_status} />
                    <span className="font-mono text-xs text-muted">
                      {utc(t.at, true)}
                    </span>
                  </div>
                  <div className="mt-1 text-[12.5px] text-muted">
                    {t.reason}
                  </div>
                </li>
              ))}
            </ol>
          </section>
        </Card>
      </div>
    </>
  );
}

function AlertRow({ alert }: { alert: Alert }) {
  const { generator_url, ...annotations } = alert.annotations;
  const source = safeHref(generator_url);
  return (
    <li className="border-b border-line-2 px-4 py-3 last:border-0">
      <div className="flex items-center gap-2.5">
        <Badge tone={alert.status === "firing" ? "crit" : "ok"}>
          {alert.status === "firing" ? "Firing" : "Resolved"}
        </Badge>
        {/* Everything below is customer data: rendered as text, never as markup. */}
        <span className="font-semibold">
          {alert.labels.alertname ?? alert.title}
        </span>
        <span className="ml-auto font-mono text-xs text-muted">
          {utc(alert.starts_at, true)}
        </span>
        {source && (
          <a
            href={source}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-brand-700 hover:underline"
          >
            Source
          </a>
        )}
      </div>
      <div className="mt-1 text-[13px]">{alert.title}</div>
      <ul className="mt-2 flex flex-wrap gap-1.5">
        {Object.entries(alert.labels).map(([k, v]) => (
          <li
            key={k}
            className="rounded border border-line bg-wash px-1.5 font-mono text-[11.5px] break-all"
          >
            {`${k}=${v}`}
          </li>
        ))}
      </ul>
      {Object.keys(annotations).length > 0 && (
        <dl className="mt-2 grid grid-cols-[120px_1fr] gap-x-3 gap-y-1 text-[12.5px]">
          {Object.entries(annotations).map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted">{k}</dt>
              <dd className="font-mono break-words whitespace-pre-wrap">{v}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}
