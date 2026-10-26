"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, FormError } from "@/components/ui/dialog";
import { api, errorMessage, unwrap } from "@/lib/api/client";

// The labels that usually name a service; preselected so the rule covers every alert of it.
const SERVICE_LABELS = new Set([
  "service",
  "service.name",
  "namespace",
  "environment",
  "env",
  "deployment.environment.name",
  "job",
  "app",
]);

function useMappings() {
  return useQuery({
    queryKey: ["service-mappings"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/service-mappings")).items,
  });
}

/** A rule from an unmapped incident's alert labels to a catalog service; files the incident too. */
export function MapServiceDialog({
  incidentId,
  labels,
  onClose,
}: {
  incidentId: string;
  labels: Record<string, string>;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [picked, setPicked] = useState(
    () => new Set(Object.keys(labels).filter((k) => SERVICE_LABELS.has(k))),
  );
  const [serviceId, setServiceId] = useState("");
  const services = useQuery({
    queryKey: ["services"],
    queryFn: async () => unwrap(await api.GET("/api/v1/services")).items,
  });
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/service-mappings", {
          body: {
            matchers: Object.fromEntries(
              Object.entries(labels).filter(([k]) => picked.has(k)),
            ),
            service_id: serviceId,
            priority: 0,
            incident_id: incidentId,
          },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["incident", incidentId] }),
        client.invalidateQueries({ queryKey: ["incidents"] }),
        client.invalidateQueries({ queryKey: ["service-mappings"] }),
      ]);
      onClose();
    },
  });

  const toggle = (key: string) =>
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <Dialog title="Map to service" onClose={onClose}>
      <div className="p-[18px]">
        <p className="mb-2 text-[13px]">Alerts with all of these labels:</p>
        <ul className="mb-4 grid gap-1">
          {Object.entries(labels).map(([k, v]) => (
            <li key={k}>
              {/* Label names and values come from the customer's alerts: text only. */}
              <label className="flex items-center gap-2 font-mono text-xs">
                <input
                  type="checkbox"
                  checked={picked.has(k)}
                  onChange={() => toggle(k)}
                  aria-label={`${k} = ${v}`}
                />
                <span className="break-all">{`${k}=${v}`}</span>
              </label>
            </li>
          ))}
        </ul>
        <label
          className="mb-1.5 block text-[13px] font-medium"
          htmlFor="map-service"
        >
          Service
        </label>
        <select
          id="map-service"
          value={serviceId}
          onChange={(e) => setServiceId(e.target.value)}
          className="h-[34px] w-full rounded-md border border-[#cfd7e0] bg-white px-2"
        >
          <option value="">Choose a service…</option>
          {(services.data ?? []).map((s) => (
            <option key={s.service_id} value={s.service_id}>
              {`${s.name} · ${s.environment}`}
            </option>
          ))}
        </select>
        <p className="mt-1.5 text-xs text-muted">
          Not listed?{" "}
          <Link href="/services" className="text-brand-700 hover:underline">
            Add it to Services
          </Link>{" "}
          and new alerts match it by name.
        </p>
        {save.isError && (
          <div className="mt-3">
            <FormError error={save.error} message={errorMessage(save.error)} />
          </div>
        )}
      </div>
      <div className="flex justify-between rounded-b-[10px] border-t border-line-2 bg-[#fbfcfd] px-[18px] py-3">
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="primary"
          disabled={picked.size === 0 || !serviceId || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? "Saving…" : "Save mapping"}
        </Button>
      </div>
    </Dialog>
  );
}

/** The tenant's mappings in the order they are tried, each with a delete. */
export function MappingsCard() {
  const client = useQueryClient();
  const mappings = useMappings();
  const remove = useMutation({
    mutationFn: async (mappingId: string) => {
      const result = await api.DELETE("/api/v1/service-mappings/{mapping_id}", {
        params: { path: { mapping_id: mappingId } },
      });
      // 204 has no body, so only a failure goes through unwrap (which throws).
      if (result.error !== undefined) unwrap(result);
    },
    onSettled: () =>
      client.invalidateQueries({ queryKey: ["service-mappings"] }),
  });
  const items = mappings.data ?? [];

  return (
    <Card>
      <section aria-label="Mappings">
        <div className="border-b border-line-2 px-4 py-3">
          <h2 className="text-[15px] font-semibold">Mappings</h2>
          <p className="text-[12.5px] text-muted">
            Alerts whose labels do not name a service are filed by these rules,
            first match wins.
          </p>
        </div>
        {mappings.isPending ? (
          <Loading rows={2} />
        ) : mappings.isError ? (
          <ErrorState
            error={mappings.error}
            onRetry={() => mappings.refetch()}
          />
        ) : items.length === 0 ? (
          <Empty
            title="No mappings"
            hint="Create one from an unmapped incident with Map to service."
          />
        ) : (
          <ul>
            {items.map((m) => (
              <li
                key={m.mapping_id}
                className="flex items-center gap-3 border-b border-line-2 px-4 py-2.5 last:border-0"
              >
                <ul className="flex min-w-0 flex-1 flex-wrap gap-1.5">
                  {Object.entries(m.matchers).map(([k, v]) => (
                    <li
                      key={k}
                      className="rounded border border-line bg-wash px-1.5 font-mono text-[11.5px] break-all"
                    >
                      {`${k}=${v}`}
                    </li>
                  ))}
                </ul>
                <span className="text-muted">→</span>
                <span className="font-semibold">
                  {m.service.name}{" "}
                  <span className="rounded border border-line bg-wash px-1.5 font-mono text-xs font-normal">
                    {m.service.environment}
                  </span>
                </span>
                <Button
                  variant="ghost"
                  aria-label={`Delete mapping to ${m.service.name}`}
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(m.mapping_id)}
                >
                  Delete
                </Button>
              </li>
            ))}
          </ul>
        )}
        {remove.isError && (
          <div className="p-4">
            <FormError
              error={remove.error}
              message={errorMessage(remove.error)}
            />
          </div>
        )}
      </section>
    </Card>
  );
}
