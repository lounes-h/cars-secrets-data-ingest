import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

export interface PipelineEvent {
  type: "run_started" | "run_finished" | "alert" | "alert_resolved" | "alert_updated" | "downloader";
  [key: string]: unknown;
}

/** Which cached queries an event makes stale. */
export function queriesFor(event: PipelineEvent): string[][] {
  switch (event.type) {
    case "run_started":
    case "run_finished":
      return [["overview"], ["runs"], ["run"], ["lots"], ["lot"], ["schema"], ["downloader"]];
    case "downloader":
      return [["downloader"]];
    case "alert":
    case "alert_resolved":
    case "alert_updated":
      return [["overview"], ["alerts"], ["run"]];
    default:
      return [["overview"]];
  }
}

/**
 * Subscribes to the server-sent event stream and refreshes whatever an event touches.
 * Returns whether the stream is currently connected (EventSource reconnects on its own).
 */
export function useLiveEvents(onEvent?: (e: PipelineEvent) => void): boolean {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    const source = new EventSource("/api/events", { withCredentials: true });
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (msg) => {
      let event: PipelineEvent;
      try {
        event = JSON.parse(msg.data);
      } catch {
        return;
      }
      for (const key of queriesFor(event)) qc.invalidateQueries({ queryKey: key });
      handler.current?.(event);
    };
    return () => source.close();
  }, [qc]);

  return connected;
}
