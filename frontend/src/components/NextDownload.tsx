import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Downloader } from "../lib/api";
import { ago, clockTime, countdown } from "../lib/format";
import { Icon } from "./ui";

const LOGIN_CMD = "cd backend && .venv/bin/python -m ingest.downloader login";
const INSTALL_CMD = "./scripts/install_downloader.sh";

export type View =
  | { kind: "countdown"; remainingMs: number; at: string; windowEnd: string | null }
  | { kind: "starting"; windowEnd: string | null }
  | { kind: "late"; at: string | null }
  | { kind: "paused"; reason: string | null | undefined }
  | { kind: "unknown"; installed: boolean };

/** What to show at `nowMs` (server-corrected), re-derived every second between refetches so
 * the display moves on by itself when the countdown reaches zero. */
export function viewAt(d: Downloader, nowMs: number): View {
  if (d.status === "paused") return { kind: "paused", reason: d.needs_attention };
  if (d.status === "unknown" || !d.next_run_at) return { kind: "unknown", installed: d.installed };
  const start = Date.parse(d.next_run_at);
  const end = d.window_end ? Date.parse(d.window_end) : start;
  if (nowMs < start) return { kind: "countdown", remainingMs: start - nowMs, at: d.next_run_at, windowEnd: d.window_end };
  if (nowMs <= end + 5 * 60_000) return { kind: "starting", windowEnd: d.window_end };
  return { kind: "late", at: d.next_run_at };
}

function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function NextDownload() {
  const q = useQuery({ queryKey: ["downloader"], queryFn: api.downloader, refetchInterval: 60_000 });
  const [skew, setSkew] = useState(0);
  useEffect(() => {
    if (q.data) setSkew(Date.parse(q.data.now) - Date.now());
  }, [q.data, q.dataUpdatedAt]);
  const now = useNow() + skew;
  if (!q.data) return null;

  const d = q.data;
  const view = viewAt(d, now);
  const jitterMin = d.jitter_seconds ? Math.round(d.jitter_seconds / 60) : 0;
  const lastOk = d.last_success_at ? <>Last successful download {ago(d.last_success_at, new Date(now))}.</> : null;

  return (
    <div className={`card next-download ${view.kind}`} aria-live="polite">
      {view.kind === "countdown" && (
        <>
          <span className="sev-info"><Icon tone="info" /></span>
          <div>
            <div className="nd-label">Next download in</div>
            <div className="nd-countdown" role="timer">{countdown(view.remainingMs)}</div>
          </div>
          <div className="small ink-2">
            Around {clockTime(view.at)}
            {jitterMin > 0 && <> (starts within {jitterMin} min after, on purpose)</>}.
            {(d.consecutive_failures ?? 0) > 0 && <> Retrying after {d.consecutive_failures} failed attempt{d.consecutive_failures === 1 ? "" : "s"}.</>}{" "}
            {lastOk}
          </div>
        </>
      )}
      {view.kind === "starting" && (
        <>
          <span className="sev-good"><Icon tone="good" /></span>
          <div>
            <div className="nd-label">Next download</div>
            <div className="nd-countdown">starting now</div>
          </div>
          <div className="small ink-2">
            Random delay, at the latest {clockTime(view.windowEnd)}. The run appears below once
            the file arrives. {lastOk}
          </div>
        </>
      )}
      {view.kind === "late" && (
        <>
          <span className="sev-warning"><Icon tone="warning" /></span>
          <div>
            <div className="nd-label">Next download</div>
            <div className="nd-countdown">overdue</div>
          </div>
          <div className="small ink-2">
            Expected around {clockTime(view.at)}, but the downloader hasn't woken up. Is the Mac
            asleep, or was the schedule removed? ({INSTALL_CMD} reinstalls it.) {lastOk}
          </div>
        </>
      )}
      {view.kind === "paused" && (
        <>
          <span className="sev-critical"><Icon tone="critical" /></span>
          <div>
            <div className="nd-label">Downloads</div>
            <div className="nd-countdown">paused</div>
          </div>
          <div className="small ink-2">
            Copart {view.reason === "challenge" ? "showed a verification page" : "needs you to log in again"}.
            Run <code>{LOGIN_CMD}</code>. {lastOk}
          </div>
        </>
      )}
      {view.kind === "unknown" && (
        <>
          <span className="muted"><Icon tone="neutral" /></span>
          <div>
            <div className="nd-label">Next download</div>
            <div className="nd-countdown">not scheduled</div>
          </div>
          <div className="small ink-2">
            {view.installed
              ? <>The downloader has not been woken by its schedule yet. Re-run <code>{INSTALL_CMD}</code> to update the schedule.</>
              : <>The downloader hasn't reported yet. Set it up with <code>{INSTALL_CMD}</code>.</>}{" "}
            {lastOk}
          </div>
        </>
      )}
    </div>
  );
}
