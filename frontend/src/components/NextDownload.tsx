import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Downloader } from "../lib/api";
import { ago, clockTime, countdown } from "../lib/format";
import { Icon } from "./ui";
import { useToast } from "./Toasts";

const LOGIN_CMD = "cd backend && .venv/bin/python -m ingest.downloader login";

export type View =
  | { kind: "countdown"; remainingMs: number; at: string }
  | { kind: "due" }
  | { kind: "downloading" }
  | { kind: "requested" }
  | { kind: "paused"; reason: string | null | undefined }
  | { kind: "not_running"; since: string | null | undefined }
  | { kind: "not_installed" };

/** What to show at `nowMs` (server-corrected), re-derived every second between refetches. */
export function viewAt(d: Downloader, nowMs: number): View {
  switch (d.status) {
    case "not_installed": return { kind: "not_installed" };
    case "paused": return { kind: "paused", reason: d.needs_attention };
    case "not_running": return { kind: "not_running", since: d.heartbeat_at };
    case "downloading": return { kind: "downloading" };
    case "requested": return { kind: "requested" };
  }
  if (!d.next_run_at) return { kind: "due" };
  const remaining = Date.parse(d.next_run_at) - nowMs;
  return remaining > 0 ? { kind: "countdown", remainingMs: remaining, at: d.next_run_at } : { kind: "due" };
}

function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

const TONE = {
  countdown: "info", due: "good", downloading: "good", requested: "good",
  paused: "critical", not_running: "critical", not_installed: "neutral",
} as const;

export function NextDownload() {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["downloader"], queryFn: api.downloader, refetchInterval: 30_000 });
  const [skew, setSkew] = useState(0);
  useEffect(() => {
    if (q.data?.now) setSkew(Date.parse(q.data.now) - Date.now());
  }, [q.data, q.dataUpdatedAt]);
  const now = useNow() + skew;
  const run = useMutation({
    mutationFn: api.downloadNow,
    onSuccess: (d) => qc.setQueryData(["downloader"], d),
    onError: (e) => toast("warning", (e as Error).message),
  });
  if (!q.data) return null;

  const d = q.data;
  const view = viewAt(d, now);
  const lastOk = d.last_success_at ? <>Last successful download {ago(d.last_success_at, new Date(now))}.</> : null;
  const canRequest = view.kind === "countdown" || view.kind === "due" || view.kind === "not_running";

  let label = "Next download";
  let big: string;
  let detail: React.ReactNode;
  switch (view.kind) {
    case "countdown":
      big = countdown(view.remainingMs);
      label = "Next download in";
      detail = <>At {clockTime(view.at)}.{(d.consecutive_failures ?? 0) > 0 && <> Retrying after {d.consecutive_failures} failed attempt{d.consecutive_failures === 1 ? "" : "s"}.</>} {lastOk}</>;
      break;
    case "due":
      big = "starting now";
      detail = <>The downloader picks it up within a few seconds. {lastOk}</>;
      break;
    case "downloading":
      big = "downloading…";
      detail = <>Chrome is fetching the file from Copart (about 100 MB); leave its window open. The run appears below once the file arrives.</>;
      break;
    case "requested":
      big = "requested";
      detail = <>Download now was requested; the downloader starts it within a few seconds.</>;
      break;
    case "paused":
      label = "Downloads";
      big = "paused";
      detail = <>Copart {view.reason === "challenge" ? "showed a verification page" : "needs you to log in again"}. Run <code>{LOGIN_CMD}</code>. {lastOk}</>;
      break;
    case "not_running":
      label = "Downloader";
      big = "not running";
      detail = <>No heartbeat {view.since ? <>since {clockTime(view.since)}</> : "yet"}. It restarts on its own (launchd) once the Mac is awake; if it doesn't, run <code>./start.sh</code>, which reinstalls it. A requested download waits until it is back. {lastOk}</>;
      break;
    default:
      label = "Downloader";
      big = "not set up";
      detail = <>Run <code>./start.sh</code>: it installs the downloader on this Mac.</>;
  }

  return (
    <div className={`card next-download ${view.kind}`} aria-live="polite">
      <span className={TONE[view.kind] === "neutral" ? "muted" : `sev-${TONE[view.kind]}`}><Icon tone={TONE[view.kind]} /></span>
      <div>
        <div className="nd-label">{label}</div>
        <div className="nd-countdown" role={view.kind === "countdown" ? "timer" : undefined}>{big}</div>
      </div>
      <div className="small ink-2">{detail}</div>
      {canRequest && (
        <button className="nd-action" disabled={run.isPending} onClick={() => run.mutate()}>
          Download now
        </button>
      )}
    </div>
  );
}
