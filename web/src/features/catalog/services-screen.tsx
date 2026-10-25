"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, PageHeader } from "@/components/ui/card";
import { Dialog, Field, FormError } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { components } from "@/generated/api";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { useIntegrations } from "../integrations/integrations-screen";

type Service = components["schemas"]["Service"];
type Criticality = NonNullable<
  components["schemas"]["NewService"]["criticality"]
>;

const CRITICALITY: Record<Criticality, Tone> = {
  critical: "crit",
  high: "warn",
  medium: "off",
  low: "off",
};
// Same alphabet the API enforces, so a name that passes here is safe in a query.
const NAME = "[A-Za-z0-9_.\\-/:]{1,128}";

export function ServicesScreen() {
  const services = useQuery({
    queryKey: ["services"],
    queryFn: async () => unwrap(await api.GET("/api/v1/services")).items,
  });
  const [editing, setEditing] = useState<Service | "new" | null>(null);
  const [importing, setImporting] = useState(false);
  const items = services.data ?? [];

  const actions = (
    <div className="flex gap-2">
      <Button onClick={() => setImporting(true)}>Import services</Button>
      <Button variant="primary" onClick={() => setEditing("new")}>
        Add service
      </Button>
    </div>
  );

  return (
    <>
      <PageHeader
        title="Services"
        subtitle="Agents can only query services listed here"
        actions={actions}
      />
      <Card>
        {services.isPending ? (
          <Loading />
        ) : services.isError ? (
          <ErrorState
            error={services.error}
            onRetry={() => services.refetch()}
          />
        ) : items.length === 0 ? (
          <Empty
            title="No services yet"
            hint="Add one, or import them from Prometheus labels."
          />
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-line bg-[#fbfcfd] text-left text-xs text-muted">
                <th className="px-4 py-2.5 font-medium">Service</th>
                <th className="px-4 py-2.5 font-medium">Environment</th>
                <th className="px-4 py-2.5 font-medium">Owner</th>
                <th className="px-4 py-2.5 font-medium">Criticality</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr
                  key={s.service_id}
                  className="border-b border-line-2 last:border-0"
                >
                  {/* Names, owners and repositories are user input: plain text, never links or markup. */}
                  <td className="px-4 py-2.5 font-semibold">{s.name}</td>
                  <td className="px-4 py-2.5">
                    <span className="rounded border border-line bg-wash px-1.5 font-mono text-xs">
                      {s.environment}
                    </span>
                  </td>
                  <td className="px-4 py-2.5">
                    {s.owner ?? <span className="text-muted">unassigned</span>}
                  </td>
                  <td className="px-4 py-2.5">
                    {s.criticality ? (
                      <Badge
                        tone={
                          CRITICALITY[s.criticality as Criticality] ?? "off"
                        }
                      >
                        {s.criticality[0].toUpperCase() +
                          s.criticality.slice(1)}
                      </Badge>
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <Button
                      variant="ghost"
                      onClick={() => setEditing(s)}
                      aria-label={`Edit ${s.name}`}
                    >
                      Edit
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {editing && (
        <ServiceDialog
          service={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
      {importing && <ImportDrawer onClose={() => setImporting(false)} />}
    </>
  );
}

function ServiceDialog({
  service,
  onClose,
}: {
  service: Service | null;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: async (body: components["schemas"]["NewService"]) =>
      service
        ? unwrap(
            await api.PATCH("/api/v1/services/{service_id}", {
              params: { path: { service_id: service.service_id } },
              body,
            }),
          )
        : unwrap(await api.POST("/api/v1/services", { body })),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["services"] });
      onClose();
    },
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const text = (key: string) => String(form.get(key) ?? "").trim() || null;
    save.mutate({
      name: text("name") ?? "",
      environment: text("environment") ?? "",
      owner: text("owner"),
      criticality: (text("criticality") as Criticality | null) ?? null,
    });
  }

  return (
    <Dialog
      title={service ? `Edit ${service.name}` : "Add service"}
      onClose={onClose}
    >
      <form onSubmit={submit}>
        <div className="p-[18px]">
          <Field label="Name" hint="As in service.name, e.g. checkout">
            <Input
              name="name"
              required
              pattern={NAME}
              defaultValue={service?.name}
              autoFocus
            />
          </Field>
          <Field
            label="Environment"
            hint="As in deployment.environment.name, e.g. prod"
          >
            <Input
              name="environment"
              required
              pattern={NAME}
              defaultValue={service?.environment}
            />
          </Field>
          <Field label="Owner (optional)">
            <Input
              name="owner"
              maxLength={200}
              defaultValue={service?.owner ?? ""}
            />
          </Field>
          <Field label="Criticality (optional)">
            <select
              name="criticality"
              defaultValue={service?.criticality ?? ""}
              className="h-[34px] w-full rounded-md border border-[#cfd7e0] bg-white px-2"
            >
              <option value="">—</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="critical">Critical</option>
            </select>
          </Field>
          {save.isError && (
            <FormError error={save.error} message={errorMessage(save.error)} />
          )}
        </div>
        <div className="flex justify-between rounded-b-[10px] border-t border-line-2 bg-[#fbfcfd] px-[18px] py-3">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function ImportDrawer({ onClose }: { onClose: () => void }) {
  const client = useQueryClient();
  const integrations = useIntegrations();
  const active = (integrations.data ?? []).filter((i) => i.status === "active");
  const [source, setSource] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const integrationId = source || active[0]?.integration_id || "";

  const suggest = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/services/import/prometheus", {
          body: { integration_id: integrationId },
        }),
      ),
    onSuccess: () => setPicked(new Set()),
  });
  const add = useMutation({
    mutationFn: async (keys: string[]) => {
      // One create per service; the catalog API has no bulk endpoint and these lists are short.
      for (const key of keys) {
        const [name, environment] = JSON.parse(key) as [string, string];
        unwrap(
          await api.POST("/api/v1/services", { body: { name, environment } }),
        );
      }
    },
    onSettled: () => client.invalidateQueries({ queryKey: ["services"] }),
    onSuccess: onClose,
  });

  const toggle = (key: string) =>
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <Dialog title="Import services" onClose={onClose} side>
      <p className="border-b border-line-2 px-4 pt-3 pb-2 text-[12.5px] text-muted">
        From Prometheus labels: <span className="font-mono">service</span>,{" "}
        <span className="font-mono">namespace</span>
      </p>
      <div className="flex gap-2 border-b border-line-2 px-4 py-3">
        <select
          aria-label="Integration"
          value={integrationId}
          onChange={(e) => setSource(e.target.value)}
          className="h-8 flex-1 rounded-md border border-[#cfd7e0] bg-white px-2"
        >
          {active.length === 0 && (
            <option value="">No active Prometheus</option>
          )}
          {active.map((i) => (
            <option key={i.integration_id} value={i.integration_id}>
              {i.name}
            </option>
          ))}
        </select>
        <Button
          onClick={() => suggest.mutate()}
          disabled={!integrationId || suggest.isPending}
        >
          {suggest.isPending ? "Looking…" : "Find services"}
        </Button>
      </div>
      <div className="flex-1 overflow-auto">
        {suggest.isError && (
          <div className="p-4">
            <FormError
              error={suggest.error}
              message={errorMessage(suggest.error)}
            />
          </div>
        )}
        {suggest.data &&
          (suggest.data.suggestions.length === 0 ? (
            <Empty title="No service labels found" />
          ) : (
            <ul>
              {suggest.data.suggestions.map((s) => {
                const key = JSON.stringify([s.name, s.environment]);
                return (
                  <li
                    key={key}
                    className="flex items-center gap-3 border-b border-line-2 px-4 py-2.5"
                  >
                    <input
                      type="checkbox"
                      aria-label={`${s.name} in ${s.environment}`}
                      checked={s.exists || picked.has(key)}
                      disabled={s.exists}
                      onChange={() => toggle(key)}
                    />
                    <span className={`flex-1 ${s.exists ? "text-muted" : ""}`}>
                      {s.name}
                    </span>
                    <span className="rounded border border-line bg-wash px-1.5 font-mono text-xs">
                      {s.environment}
                    </span>
                    {s.exists && <Badge tone="off">Added</Badge>}
                  </li>
                );
              })}
            </ul>
          ))}
        {suggest.data?.truncated && (
          <p className="px-4 py-2 text-xs text-muted">Showing the first 200.</p>
        )}
        {add.isError && (
          <div className="p-4">
            <FormError error={add.error} message={errorMessage(add.error)} />
          </div>
        )}
      </div>
      <div className="flex items-center justify-between border-t border-line-2 bg-[#fbfcfd] px-[18px] py-3">
        <span className="text-[12.5px] text-muted">{picked.size} selected</span>
        <Button
          variant="primary"
          disabled={picked.size === 0 || add.isPending}
          onClick={() => add.mutate([...picked])}
        >
          {add.isPending ? "Adding…" : "Add services"}
        </Button>
      </div>
    </Dialog>
  );
}
