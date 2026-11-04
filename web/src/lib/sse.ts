"use client";

import { useEffect, useRef, useState } from "react";

export type FeedEvent = {
  id: string;
  type: string;
  incident_id: string;
  ref_id: string | null;
  at: string;
};

const TYPES = [
  "incident.opened",
  "incident.event_attached",
  "incident.transition",
  "incident.updated",
  "incident.notified",
];
// The API ends streams when the token expires and the browser reconnects within seconds;
// only an outage that outlasts this is worth a banner.
const GRACE_MS = 10_000;
const RETRY_MS = 3000;
// Signed out or no access: retrying only repeats the answer.
const REFUSED = [401, 403, 404];

export type StreamState = "live" | "reconnecting" | "stopped";

/** Follows a same-origin event stream; the browser reconnects and resumes from Last-Event-ID. */
export function useEventStream(
  path: string,
  onEvent: (event: FeedEvent) => void,
): StreamState {
  const [state, setState] = useState<StreamState>("live");
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  });

  useEffect(() => {
    let source: EventSource;
    let lastId = "";
    let grace: ReturnType<typeof setTimeout> | undefined;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const url = () => (lastId ? `${path}?last_event_id=${lastId}` : path);
    const receive = (message: MessageEvent<string>) => {
      lastId = message.lastEventId;
      try {
        handler.current({
          id: message.lastEventId,
          ...JSON.parse(message.data),
        });
      } catch {
        // A malformed message is skipped; the next one carries on.
      }
    };
    const connect = () => {
      source?.close();
      source = new EventSource(url());
      source.addEventListener("ready", (message) => {
        lastId = (message as MessageEvent<string>).lastEventId;
      });
      for (const type of TYPES) source.addEventListener(type, receive);
      source.onopen = () => {
        clearTimeout(grace);
        grace = undefined;
        setState("live");
      };
      source.onerror = () => {
        grace ??= setTimeout(() => setState("reconnecting"), GRACE_MS);
        // CLOSED: a failed response ended the browser's own retries, so they are ours now.
        if (source.readyState === EventSource.CLOSED)
          retry = setTimeout(probe, RETRY_MS);
      };
    };
    // EventSource hides the status of a failed response; this request shows it.
    const probe = async () => {
      const status = await fetch(url())
        .then((response) => {
          void response.body?.cancel();
          return response.status;
        })
        .catch(() => 0);
      if (REFUSED.includes(status)) {
        clearTimeout(grace);
        setState("stopped");
      } else if (status === 200) connect();
      else retry = setTimeout(probe, RETRY_MS);
    };
    connect();
    return () => {
      clearTimeout(grace);
      clearTimeout(retry);
      source.close();
    };
  }, [path]);

  return state;
}
