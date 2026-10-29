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
];
// The API ends streams when the token expires and the browser reconnects within seconds;
// only an outage that outlasts this is worth a banner.
const GRACE_MS = 10_000;

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
    const source = new EventSource(path);
    let grace: ReturnType<typeof setTimeout> | undefined;
    const receive = (message: MessageEvent<string>) => {
      try {
        handler.current({
          id: message.lastEventId,
          ...JSON.parse(message.data),
        });
      } catch {
        // A malformed message is skipped; the next one carries on.
      }
    };
    for (const type of TYPES) source.addEventListener(type, receive);
    source.onopen = () => {
      clearTimeout(grace);
      setState("live");
    };
    source.onerror = () => {
      clearTimeout(grace);
      // CLOSED means the server refused (signed out, no access): the browser will not retry.
      if (source.readyState === EventSource.CLOSED) setState("stopped");
      else grace = setTimeout(() => setState("reconnecting"), GRACE_MS);
    };
    return () => {
      clearTimeout(grace);
      source.close();
    };
  }, [path]);

  return state;
}
