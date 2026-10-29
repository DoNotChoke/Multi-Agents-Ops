"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import type { components } from "@/generated/api";
import { api } from "@/lib/api/client";
import { useEventStream } from "@/lib/sse";
import { SeverityBadge } from "./incidents-screen";

type Incident = components["schemas"]["Incident"];

const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function where(i: Incident) {
  const service = i.service ?? i.unmapped_service;
  return [service?.name, service?.environment].filter(Boolean).join(" · ");
}

/** A system notification while the tab is in the background; plain text only. */
function notifyDesktop(i: Incident, open: (href: string) => void) {
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "granted" || !document.hidden) return;
  const n = new Notification(`${capital(i.severity)}: ${i.title}`, {
    body: where(i),
    tag: i.incident_id,
  });
  n.onclick = () => {
    window.focus();
    open(`/incidents/${i.incident_id}`);
  };
}

/** Keeps incident data live from the tenant stream and announces new incidents. */
export function LiveIncidents() {
  const client = useQueryClient();
  const router = useRouter();
  const [toasts, setToasts] = useState<Incident[]>([]);
  const [unseen, setUnseen] = useState(0);
  const [permission, setPermission] = useState(() =>
    typeof Notification === "undefined"
      ? "unsupported"
      : Notification.permission,
  );

  const state = useEventStream("/api/v1/stream", async (event) => {
    void client.invalidateQueries({ queryKey: ["incidents"] });
    void client.invalidateQueries({
      queryKey: ["incident", event.incident_id],
    });
    if (event.type !== "incident.opened") return;
    // The stream carries ids only; the content comes through the API's access checks.
    const data = await api
      .GET("/api/v1/incidents/{incident_id}", {
        params: { path: { incident_id: event.incident_id } },
      })
      .then((r) => r.data)
      .catch(() => undefined); // the list still refreshes; only the toast is lost
    if (!data) return;
    setToasts((current) =>
      [
        data,
        ...current.filter((t) => t.incident_id !== data.incident_id),
      ].slice(0, 3),
    );
    setUnseen((n) => n + 1);
    notifyDesktop(data, router.push);
  });

  return (
    <>
      <div className="ml-auto flex items-center gap-1.5">
        {permission === "default" && (
          <button
            type="button"
            onClick={async () =>
              setPermission(await Notification.requestPermission())
            }
            className="text-[11px] text-brand-700 hover:underline"
          >
            Enable desktop alerts
          </button>
        )}
        <Link
          href="/incidents"
          onClick={() => setUnseen(0)}
          aria-label={
            unseen === 0
              ? "No new incidents"
              : `${unseen} new incident${unseen === 1 ? "" : "s"}`
          }
          className="relative grid size-7 place-items-center rounded-md text-ink-2 hover:bg-wash"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className="size-4"
            aria-hidden
          >
            <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10 21h4" />
          </svg>
          {unseen > 0 && (
            <span className="absolute -top-0.5 -right-0.5 min-w-4 rounded-full bg-crit px-1 text-center text-[10px] leading-4 font-semibold text-white">
              {unseen > 9 ? "9+" : unseen}
            </span>
          )}
        </Link>
      </div>

      {state !== "live" && (
        <div
          role="alert"
          className="fixed top-3 left-1/2 z-50 -translate-x-1/2 rounded-md border border-warn/40 bg-warn-bg px-3 py-1.5 text-[13px] shadow"
        >
          {state === "stopped"
            ? "Live updates stopped. Reload the page to resume."
            : "Live updates disconnected. Reconnecting…"}
        </div>
      )}

      <section
        aria-label="New incidents"
        aria-live="polite"
        className="fixed right-5 bottom-5 z-50 grid w-[340px] gap-2"
      >
        {toasts.map((i) => (
          <div
            key={i.incident_id}
            className="rounded-[10px] border border-line bg-white px-3.5 py-3 shadow-lg"
          >
            <div className="mb-1 flex items-center gap-2">
              <SeverityBadge severity={i.severity} />
              <span className="text-xs text-muted">New incident</span>
              <button
                type="button"
                aria-label="Dismiss"
                onClick={() =>
                  setToasts((current) =>
                    current.filter((t) => t.incident_id !== i.incident_id),
                  )
                }
                className="ml-auto text-muted hover:text-ink"
              >
                ×
              </button>
            </div>
            {/* Titles come from customer alerts: text only. */}
            <Link
              href={`/incidents/${i.incident_id}`}
              className="block font-semibold break-words hover:underline"
            >
              {i.title}
            </Link>
            <div className="text-[12.5px] text-muted">{where(i)}</div>
          </div>
        ))}
      </section>
    </>
  );
}
