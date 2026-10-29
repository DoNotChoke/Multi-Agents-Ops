"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, PageHeader } from "@/components/ui/card";
import { Dialog, Field, FormError } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { components } from "@/generated/api";
import { api, errorMessage, unwrap } from "@/lib/api/client";

type Integration = components["schemas"]["Integration"];
type TestReport = components["schemas"]["TestReport"];
type Created = components["schemas"]["CreatedIntegration"];
// The integration type decides how the secret is explained: inbound bearer or outbound signing.
type Revealed = components["schemas"]["WebhookSecret"] & { type: string };
type Kind = "prometheus" | "alertmanager" | "webhook";

const KIND_LABELS: Record<Kind, string> = {
  prometheus: "Prometheus",
  alertmanager: "Alertmanager",
  webhook: "Webhook",
};

const CHECK_LABELS: Record<string, string> = {
  endpoint_allowed: "Endpoint allowed",
  credentials_readable: "Credentials",
  reachable: "Reachable",
  query_up: "Query up",
  delivered: "Sample sent",
};

export function useIntegrations() {
  return useQuery({
    queryKey: ["integrations"],
    queryFn: async () => unwrap(await api.GET("/api/v1/integrations")).items,
  });
}

export function IntegrationsScreen() {
  const integrations = useIntegrations();
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [reports, setReports] = useState<Record<string, TestReport>>({});
  const [revealed, setRevealed] = useState<Revealed | null>(null);
  const [rotating, setRotating] = useState<Integration | null>(null);

  const test = useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await api.POST("/api/v1/integrations/{integration_id}/test", {
          params: { path: { integration_id: id } },
        }),
      ),
    onSuccess: (report, id) => setReports((r) => ({ ...r, [id]: report })),
  });

  const items = integrations.data ?? [];
  const current = items.find((i) => i.integration_id === selected) ?? items[0];
  const add = (
    <Button variant="primary" onClick={() => setAdding(true)}>
      Add integration
    </Button>
  );

  return (
    <>
      <PageHeader
        title="Integrations"
        subtitle="Data sources agents can read, and where incidents are announced"
        actions={add}
      />
      {integrations.isPending ? (
        <Card>
          <Loading />
        </Card>
      ) : integrations.isError ? (
        <Card>
          <ErrorState
            error={integrations.error}
            onRetry={() => integrations.refetch()}
          />
        </Card>
      ) : items.length === 0 ? (
        <Card>
          <Empty
            title="No integrations yet"
            hint="Connect Prometheus to get started."
            action={add}
          />
        </Card>
      ) : (
        <div className="grid grid-cols-[1fr_400px] items-start gap-4">
          <Card>
            <table className="w-full">
              <thead>
                <tr className="border-b border-line bg-[#fbfcfd] text-left text-xs text-muted">
                  <th className="px-4 py-2.5 font-medium">Name</th>
                  <th className="px-4 py-2.5 font-medium">Type</th>
                  <th className="px-4 py-2.5 font-medium">Endpoint</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {items.map((i) => (
                  <tr
                    key={i.integration_id}
                    onClick={() => setSelected(i.integration_id)}
                    aria-selected={i.integration_id === current?.integration_id}
                    className="cursor-pointer border-b border-line-2 last:border-0 hover:bg-[#fafbfd] aria-selected:bg-brand-50"
                  >
                    <td className="px-4 py-2.5 font-semibold">{i.name}</td>
                    <td className="px-4 py-2.5 capitalize">{i.type}</td>
                    <td className="px-4 py-2.5 font-mono text-xs">
                      {String(i.config.endpoint ?? "")}
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge tone={i.status === "active" ? "ok" : "off"}>
                        {i.status === "active" ? "Active" : "Disabled"}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          {current && (
            <Detail
              integration={current}
              report={reports[current.integration_id]}
              testing={
                test.isPending && test.variables === current.integration_id
              }
              error={
                test.variables === current.integration_id ? test.error : null
              }
              onTest={() => test.mutate(current.integration_id)}
              onRotate={() => setRotating(current)}
            />
          )}
        </div>
      )}
      {adding && (
        <AddIntegration
          onClose={() => setAdding(false)}
          onCreated={(created) => {
            setAdding(false);
            setSelected(created.integration_id);
            if (created.webhook_secret)
              setRevealed({
                type: created.type,
                webhook_url: created.webhook_url ?? "",
                webhook_secret: created.webhook_secret,
              });
            else test.mutate(created.integration_id);
          }}
        />
      )}
      {rotating && (
        <RotateSecret
          integration={rotating}
          onClose={() => setRotating(null)}
          onRotated={(webhook) => {
            setRotating(null);
            setRevealed({ ...webhook, type: rotating.type });
          }}
        />
      )}
      {revealed && (
        <RevealSecret webhook={revealed} onClose={() => setRevealed(null)} />
      )}
    </>
  );
}

function Detail({
  integration,
  report,
  testing,
  error,
  onTest,
  onRotate,
}: {
  integration: Integration;
  report?: TestReport;
  testing: boolean;
  error: unknown;
  onTest: () => void;
  onRotate: () => void;
}) {
  const mapping = (integration.config.metric_mapping ?? {}) as Record<
    string,
    string
  >;
  return (
    <Card>
      <div className="flex items-center justify-between border-b border-line-2 px-4 py-3">
        <div className="min-w-0">
          <h2 className="truncate text-[15px] font-semibold">
            {integration.name}
          </h2>
          <div className="font-mono text-xs text-muted">
            {integration.integration_id}
          </div>
        </div>
        <Button onClick={onTest} disabled={testing}>
          {testing ? "Testing…" : "Test"}
        </Button>
      </div>
      <div className="grid gap-4 px-4 py-3.5">
        <section aria-label="Connection test">
          <h3 className="mb-1 text-xs font-semibold tracking-wider text-ink-2 uppercase">
            Connection test
          </h3>
          {error ? (
            <FormError error={error} message={errorMessage(error)} />
          ) : report ? (
            <ul>
              {report.checks.map((c) => (
                <li
                  key={c.name}
                  className="grid grid-cols-[22px_130px_1fr] items-center gap-2.5 border-b border-line-2 py-2 last:border-0"
                >
                  <span
                    aria-label={c.ok ? "passed" : "failed"}
                    className={`size-[18px] rounded-full ${c.ok ? "bg-ok" : "bg-crit"}`}
                  />
                  <span>{CHECK_LABELS[c.name] ?? c.name}</span>
                  {/* Server text can echo the customer's system; it is rendered as text only. */}
                  <span className="truncate text-muted">{c.detail}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted">Not tested yet.</p>
          )}
        </section>
        {integration.type === "webhook" ? (
          <section aria-label="Notification channel">
            <h3 className="mb-1 text-xs font-semibold tracking-wider text-ink-2 uppercase">
              Notification channel
            </h3>
            <div className="mb-2 font-mono text-xs break-all">
              {String(integration.config.endpoint ?? "")}
            </div>
            <Button onClick={onRotate}>Rotate secret</Button>
          </section>
        ) : integration.webhook_url ? (
          <section aria-label="Webhook">
            <h3 className="mb-1 text-xs font-semibold tracking-wider text-ink-2 uppercase">
              Webhook
            </h3>
            <div className="mb-2 font-mono text-xs break-all">
              {integration.webhook_url}
            </div>
            <Button onClick={onRotate}>Rotate secret</Button>
          </section>
        ) : (
          <section className="flex items-center justify-between">
            <div>
              <h3 className="text-xs font-semibold tracking-wider text-ink-2 uppercase">
                Metric mapping
              </h3>
              <div className="text-[12.5px] text-muted">
                {Object.keys(mapping).length} of 6 templates mapped
              </div>
            </div>
            <Badge tone={integration.has_credentials ? "info" : "off"}>
              {integration.has_credentials
                ? "Credentials stored"
                : "No credentials"}
            </Badge>
          </section>
        )}
      </div>
    </Card>
  );
}

function AddIntegration({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (i: Created) => void;
}) {
  const client = useQueryClient();
  const [kind, setKind] = useState<Kind>("prometheus");
  const [mappingError, setMappingError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: async (
      body:
        | components["schemas"]["NewPrometheus"]
        | components["schemas"]["NewAlertmanager"]
        | components["schemas"]["NewWebhook"],
    ) => unwrap(await api.POST("/api/v1/integrations", { body })),
    onSuccess: async (created) => {
      await client.invalidateQueries({ queryKey: ["integrations"] });
      onCreated(created);
    },
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); // keep the typed values when the server refuses them
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    const endpoint = String(form.get("endpoint") ?? "").trim();
    if (kind === "webhook") {
      create.mutate({
        type: "webhook",
        mode: "direct",
        name,
        config: { endpoint },
      });
      return;
    }
    if (kind === "alertmanager") {
      create.mutate({ type: "alertmanager", mode: "direct", name });
      return;
    }
    const raw = String(form.get("mapping") ?? "").trim();
    let mapping: Record<string, string> = {};
    if (raw) {
      try {
        mapping = JSON.parse(raw);
      } catch {
        setMappingError("Metric mapping must be a JSON object.");
        return;
      }
    }
    setMappingError(null);
    const token = String(form.get("token") ?? "");
    create.mutate({
      type: "prometheus",
      mode: "direct",
      name,
      config: {
        endpoint,
        metric_mapping: mapping,
      },
      ...(token ? { credentials: token } : {}),
    });
  }

  return (
    <Dialog title={`Add ${KIND_LABELS[kind]}`} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="p-[18px]">
          <fieldset className="mb-3.5 flex gap-2">
            <legend className="sr-only">Type</legend>
            {(Object.keys(KIND_LABELS) as Kind[]).map((k) => (
              <label
                key={k}
                className="flex cursor-pointer items-center gap-1.5 rounded-md border border-line px-2.5 py-1.5 has-checked:border-brand-700 has-checked:bg-brand-50"
              >
                <input
                  type="radio"
                  name="kind"
                  value={k}
                  checked={kind === k}
                  onChange={() => setKind(k)}
                />
                {KIND_LABELS[k]}
              </label>
            ))}
          </fieldset>
          <Field label="Name">
            <Input
              name="name"
              required
              pattern="[A-Za-z0-9_.\-]{1,100}"
              autoFocus
            />
          </Field>
          {kind === "webhook" ? (
            <>
              <Field label="Endpoint">
                <Input
                  name="endpoint"
                  required
                  placeholder="https://hooks.example.com/opsteam"
                  className="font-mono text-[13px]"
                />
              </Field>
              <p className="text-[12.5px] text-muted">
                HTTPS only. Every new incident is POSTed here as JSON, signed
                with a secret you get next.
              </p>
            </>
          ) : kind === "alertmanager" ? (
            <p className="text-[12.5px] text-muted">
              You get a webhook URL and secret to put in your Alertmanager
              config.
            </p>
          ) : (
            <>
              <Field label="Endpoint">
                <Input
                  name="endpoint"
                  required
                  placeholder="https://prometheus.example.com"
                  className="font-mono text-[13px]"
                />
              </Field>
              <Field
                label="Bearer token (optional)"
                hint="Encrypted at rest and never shown again."
              >
                <Input name="token" type="password" autoComplete="off" />
              </Field>
              <Field
                label="Metric mapping (optional)"
                hint="JSON: template name to PromQL using ${service} and ${environment}."
              >
                <textarea
                  name="mapping"
                  rows={3}
                  className="w-full rounded-md border border-[#cfd7e0] px-2.5 py-2 font-mono text-xs"
                />
              </Field>
            </>
          )}
          {mappingError && <FormError error={null} message={mappingError} />}
          {create.isError && (
            <FormError
              error={create.error}
              message={errorMessage(create.error)}
            />
          )}
        </div>
        <div className="flex justify-between rounded-b-[10px] border-t border-line-2 bg-[#fbfcfd] px-[18px] py-3">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending
              ? "Saving…"
              : kind === "alertmanager"
                ? "Create webhook"
                : kind === "webhook"
                  ? "Create channel"
                  : "Save and test"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function RotateSecret({
  integration,
  onClose,
  onRotated,
}: {
  integration: Integration;
  onClose: () => void;
  onRotated: (webhook: components["schemas"]["WebhookSecret"]) => void;
}) {
  const rotate = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST(
          "/api/v1/integrations/{integration_id}/webhook-secret/rotate",
          { params: { path: { integration_id: integration.integration_id } } },
        ),
      ),
    onSuccess: onRotated,
  });
  return (
    <Dialog title="Rotate webhook secret" onClose={onClose}>
      <div className="p-[18px]">
        {integration.type === "webhook" ? (
          <p>
            Messages to <strong>{integration.name}</strong> are signed with the
            new secret at once; update your receiver to verify with it.
          </p>
        ) : (
          <p>
            The current secret stops working at once. Alerts from{" "}
            <strong>{integration.name}</strong> are refused until you put the
            new secret in your Alertmanager config.
          </p>
        )}
        {rotate.isError && (
          <FormError
            error={rotate.error}
            message={errorMessage(rotate.error)}
          />
        )}
      </div>
      <div className="flex justify-between rounded-b-[10px] border-t border-line-2 bg-[#fbfcfd] px-[18px] py-3">
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="primary"
          disabled={rotate.isPending}
          onClick={() => rotate.mutate()}
        >
          {rotate.isPending ? "Rotating…" : "Rotate secret"}
        </Button>
      </div>
    </Dialog>
  );
}

function RevealSecret({
  webhook,
  onClose,
}: {
  webhook: Revealed;
  onClose: () => void;
}) {
  const signing = webhook.type === "webhook";
  const config = signing
    ? webhook.webhook_secret
    : `receivers:
  - name: opsteam
    webhook_configs:
      - url: ${webhook.webhook_url}
        send_resolved: true
        http_config:
          authorization:
            credentials: ${webhook.webhook_secret}`;
  const label = signing ? "Signing secret" : "Alertmanager receiver";
  return (
    <Dialog
      title={signing ? "Signing secret" : "Webhook secret"}
      onClose={onClose}
    >
      <div className="p-[18px]">
        <p className="mb-3 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[12.5px]">
          Copy it now. It won&apos;t be shown again.
        </p>
        <h3 className="mb-1.5 text-[13px] font-medium">{label}</h3>
        <pre
          aria-label={label}
          className="overflow-x-auto rounded-md border border-line bg-wash px-3 py-2 font-mono text-xs"
        >
          {config}
        </pre>
        {signing && (
          <p className="mt-2 text-[12.5px] text-muted">
            Each request carries <code>X-Opsteam-Timestamp</code> and{" "}
            <code>X-Opsteam-Signature: sha256=…</code>, an HMAC-SHA256 of the
            timestamp, a dot and the raw body. Reject old timestamps.
          </p>
        )}
      </div>
      <div className="flex justify-between rounded-b-[10px] border-t border-line-2 bg-[#fbfcfd] px-[18px] py-3">
        <Button onClick={() => navigator.clipboard.writeText(config)}>
          Copy
        </Button>
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      </div>
    </Dialog>
  );
}
