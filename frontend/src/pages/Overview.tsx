import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { RunCharts } from "../components/RunCharts";
import { Icon, QueryState, RunStatusBadge, SeverityBadge, StatTile } from "../components/ui";
import { api, type Downloader, type Overview } from "../lib/api";
import { ago, clockTime, compact, dateTime, int } from "../lib/format";

const HEALTH = {
  healthy: { tone: "good", title: "Pipeline healthy", text: "No open warnings or critical alerts." },
  degraded: { tone: "warning", title: "Pipeline degraded", text: "Open warnings need a look." },
  down: { tone: "critical", title: "Pipeline needs attention", text: "Critical alerts are open, or the worker or downloader is not running." },
} as const;

type Tone = "good" | "warning" | "critical" | "info" | "neutral";

/** One line per background process: [tone, text]. */
export function downloaderLine(d: Downloader): [Tone, ReactNode] {
  switch (d.status) {
    case "scheduled":
      return ["good", <>Downloader running · next download at {clockTime(d.next_run_at)}</>];
    case "downloading":
      return ["good", <>Downloader running · downloading now</>];
    case "requested":
      return ["good", <>Downloader running · download requested</>];
    case "paused":
      return ["critical", <>Downloader paused · Copart needs you to log in again</>];
    case "not_running":
      return ["critical", <>Downloader not running{d.heartbeat_at ? <> since {clockTime(d.heartbeat_at)}</> : null} · it restarts on its own; otherwise run <code>./start.sh</code></>];
    default:
      return ["neutral", <>Downloader not set up · run <code>./start.sh</code> to install it</>];
  }
}

function StatusLine({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <div className="ink-2 small status-line">
      <span className={tone === "neutral" ? "muted" : `sev-${tone}`}><Icon tone={tone} /></span>
      <span>{children}</span>
    </div>
  );
}

function HealthBanner({ data }: { data: Overview }) {
  const h = HEALTH[data.health];
  const worker = data.worker;
  const [dlTone, dlText] = downloaderLine(data.downloader);
  return (
    <div className={`banner ${data.health}`}>
      <span className={`sev-${h.tone}`}><Icon tone={h.tone} /></span>
      <div style={{ flex: 1 }} className="banner-lines">
        <strong>{h.title}</strong>
        <div className="ink-2 small">{h.text}</div>
        <StatusLine tone={worker?.healthy ? "good" : "critical"}>
          {worker
            ? <>Worker {worker.healthy ? "running" : "not running"} · last heartbeat {ago(worker.last_beat_at)} · {worker.state}</>
            : <>No worker has reported yet.</>}
        </StatusLine>
        <StatusLine tone={dlTone}>
          <Link to="/runs" className="plain-link">{dlText}</Link>
        </StatusLine>
      </div>
      {(data.alerts_open.critical > 0 || data.alerts_open.warning > 0) && (
        <Link to="/alerts">
          {data.alerts_open.critical > 0 && `${data.alerts_open.critical} critical`}
          {data.alerts_open.critical > 0 && data.alerts_open.warning > 0 && ", "}
          {data.alerts_open.warning > 0 && `${data.alerts_open.warning} warning`} →
        </Link>
      )}
    </div>
  );
}

export function OverviewPage() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 30_000 });
  const runs = useQuery({ queryKey: ["runs", "chart"], queryFn: () => api.runs({ limit: 48 }) });
  const alerts = useQuery({ queryKey: ["alerts", "overview"], queryFn: () => api.alerts({ limit: 5 }) });

  if (!overview.data) return <QueryState isLoading={overview.isLoading} error={overview.error} />;
  const data = overview.data;

  return (
    <div className="stack">
      <div className="topbar"><h1>Overview</h1></div>
      <HealthBanner data={data} />

      <div className="grid tiles">
        <StatTile label="Active lots" value={compact(data.lots.active)}
          sub={`${compact(data.lots.removed)} removed overall`} />
        <StatTile label="New lots, last 24 h" value={compact(data.lots.new_24h)} />
        <StatTile label="Changed lots, last 24 h" value={compact(data.lots.changed_24h)} />
        <StatTile label="Removed lots, last 24 h" value={compact(data.lots.removed_24h)} />
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Provider</th><th>Last file</th><th>Status</th><th>Received</th>
              <th className="num">Rows</th><th className="num">New</th><th className="num">Updated</th>
              <th>Last success</th><th className="num">Active lots</th>
            </tr>
          </thead>
          <tbody>
            {data.providers.map((p) => (
              <tr key={p.source}>
                <td><strong>{p.display_name}</strong></td>
                <td className="wrap-anywhere">
                  {p.last_run ? <Link to={`/runs/${p.last_run.id}`}>{p.last_run.filename}</Link> : <span className="muted">none yet</span>}
                </td>
                <td>{p.last_run && <RunStatusBadge status={p.last_run.status} />}</td>
                <td>{p.last_run ? dateTime(p.last_run.received_at) : "—"}</td>
                <td className="num">{int(p.last_run?.rows_total)}</td>
                <td className="num">{int(p.last_run?.rows_new)}</td>
                <td className="num">{int(p.last_run?.rows_updated)}</td>
                <td title={`Alert after ${p.stale_after_minutes} min without a file`}>{ago(p.last_success_at)}</td>
                <td className="num">{int(p.lots_active)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {runs.data && <RunCharts runs={runs.data.items} />}

      <div className="card">
        <div className="topbar" style={{ marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>Open alerts</h2>
          <Link to="/alerts">All alerts →</Link>
        </div>
        {alerts.data?.items.length ? (
          alerts.data.items.map((a) => (
            <div key={a.id} className="alert-row" style={{ padding: "8px 0" }}>
              <SeverityBadge severity={a.severity} />
              <span className="alert-title">{a.title}</span>
              <span className="muted small">{ago(a.last_seen_at)}</span>
            </div>
          ))
        ) : (
          <div className="muted">Nothing open.</div>
        )}
      </div>
    </div>
  );
}
