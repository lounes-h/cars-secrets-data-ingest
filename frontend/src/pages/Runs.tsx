import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { NextDownload } from "../components/NextDownload";
import { useToast } from "../components/Toasts";
import { Pager, QueryState, RunStatusBadge, Segmented, SeverityBadge } from "../components/ui";
import { api, type RunStatus } from "../lib/api";
import { bytes, dateTime, duration, int } from "../lib/format";

const LIMIT = 50;
type StatusFilter = "" | RunStatus;

export function RunsPage() {
  const navigate = useNavigate();
  const [status, setStatus] = useState<StatusFilter>("");
  const [offset, setOffset] = useState(0);
  const q = useQuery({
    queryKey: ["runs", status, offset],
    queryFn: () => api.runs({ status, limit: LIMIT, offset }),
  });

  return (
    <div>
      <div className="topbar"><h1>Runs</h1></div>
      <NextDownload />
      <div className="filters">
        <Segmented<StatusFilter> label="Status" value={status}
          onChange={(v) => { setStatus(v); setOffset(0); }}
          options={[
            { value: "", label: "All" }, { value: "succeeded", label: "Succeeded" },
            { value: "failed", label: "Failed" }, { value: "duplicate", label: "Duplicate" },
            { value: "interrupted", label: "Interrupted" },
          ]} />
      </div>
      <QueryState isLoading={q.isLoading} error={q.error} />
      {q.data && (
        <>
          <div className="table-wrap" style={{ opacity: q.isFetching ? 0.7 : 1 }}>
            <table>
              <thead>
                <tr>
                  <th>#</th><th>Provider</th><th>File</th><th>Status</th><th>Received</th>
                  <th className="num">Rows</th><th className="num">New</th><th className="num">Updated</th>
                  <th className="num">Removed</th><th className="num">Rejected</th><th className="num">Duration</th>
                </tr>
              </thead>
              <tbody>
                {q.data.items.map((r) => (
                  <tr key={r.id} className="clickable" onClick={() => navigate(`/runs/${r.id}`)}>
                    <td><Link to={`/runs/${r.id}`}>{r.id}</Link></td>
                    <td>{r.source}</td>
                    <td className="wrap-anywhere">{r.filename}</td>
                    <td><RunStatusBadge status={r.status} /></td>
                    <td>{dateTime(r.received_at)}</td>
                    <td className="num">{int(r.rows_total)}</td>
                    <td className="num">{int(r.rows_new)}</td>
                    <td className="num">{int(r.rows_updated)}</td>
                    <td className="num">{int(r.rows_removed)}</td>
                    <td className="num">{int(r.rows_rejected)}</td>
                    <td className="num">{duration(r.duration_seconds)}</td>
                  </tr>
                ))}
                {q.data.items.length === 0 && (
                  <tr><td colSpan={11} className="empty">No runs yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} limit={LIMIT} total={q.data.total} onChange={setOffset} />
        </>
      )}
    </div>
  );
}

export function RunDetailPage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["run", id], queryFn: () => api.run(id) });
  const retry = useMutation({
    mutationFn: () => api.retryRun(id),
    onSuccess: (r) => {
      toast("info", `${r.file} is back in the inbox and will be ingested again.`);
      qc.invalidateQueries({ queryKey: ["run", id] });
    },
    onError: (e) => toast("warning", (e as Error).message),
  });

  if (!q.data) return <QueryState isLoading={q.isLoading} error={q.error} />;
  const { run, rejected_rows, alerts } = q.data;
  const fieldErrors = Object.entries(run.field_errors ?? {});

  return (
    <div className="stack">
      <div className="topbar">
        <h1>Run #{run.id} <RunStatusBadge status={run.status} /></h1>
        {run.status === "failed" && run.archived_path && (
          <button className="primary" disabled={retry.isPending} onClick={() => retry.mutate()}>
            Retry this file
          </button>
        )}
      </div>

      <div className="grid two-col">
        <div className="card">
          <h2>File</h2>
          <dl className="kv">
            <dt>Provider</dt><dd>{run.source}</dd>
            <dt>Name</dt><dd>{run.filename}</dd>
            <dt>Size</dt><dd>{bytes(run.size_bytes)}</dd>
            <dt>SHA-256</dt><dd className="mono small">{run.sha256}</dd>
            <dt>Received</dt><dd>{dateTime(run.received_at)}</dd>
            <dt>Duration</dt><dd>{duration(run.duration_seconds)}</dd>
            <dt>Stored at</dt><dd className="mono small">{run.archived_path ?? "—"}</dd>
            {run.error && (<><dt>Error</dt><dd className="error">{run.error}</dd></>)}
          </dl>
        </div>
        <div className="card">
          <h2>Rows</h2>
          <dl className="kv">
            <dt>In file</dt><dd>{int(run.rows_total)}</dd>
            <dt>New lots</dt><dd>{int(run.rows_new)}</dd>
            <dt>Updated lots</dt><dd>{int(run.rows_updated)}</dd>
            <dt>Unchanged lots</dt><dd>{int(run.rows_unchanged)}</dd>
            <dt>Marked removed</dt><dd>{int(run.rows_removed)}</dd>
            {!!run.rows_removal_deferred && (
              <>
                <dt>Removal deferred</dt>
                <dd title="Missing from this file, but the auction (sale time + grace period) isn't over yet">
                  {int(run.rows_removal_deferred)} lots (auction not over yet)
                </dd>
              </>
            )}
            <dt>Rejected rows</dt><dd>{int(run.rows_rejected)}</dd>
            {run.removal_skipped.length > 0 && (
              <>
                <dt>Removal skipped</dt>
                <dd title="These yards held under half their active lots, so missing lots were not marked removed">
                  {run.removal_skipped.length} yard(s): {run.removal_skipped.slice(0, 12).join(", ")}
                  {run.removal_skipped.length > 12 && "…"}
                </dd>
              </>
            )}
            {fieldErrors.length > 0 && (
              <>
                <dt>Unparseable values</dt>
                <dd>{fieldErrors.map(([k, v]) => `${k}: ${int(v)}`).join(", ")}</dd>
              </>
            )}
          </dl>
        </div>
      </div>

      {alerts.length > 0 && (
        <div className="card">
          <h2>Alerts from this run</h2>
          {alerts.map((a) => (
            <div key={a.id} className="alert-row" style={{ padding: "6px 0" }}>
              <SeverityBadge severity={a.severity} />
              <span className="alert-title">{a.title}</span>
              <span className="muted small">{a.resolved_at ? "resolved" : "open"}</span>
            </div>
          ))}
        </div>
      )}

      <div>
        <h2>Rejected rows {rejected_rows.length > 0 && <span className="muted">(first {rejected_rows.length})</span>}</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th className="num">Line</th><th>Reason</th><th>Raw values</th></tr></thead>
            <tbody>
              {rejected_rows.map((r) => (
                <tr key={r.line_no}>
                  <td className="num">{r.line_no}</td>
                  <td>{r.reason}</td>
                  <td className="mono small wrap-anywhere">{r.raw.slice(0, 12).join(" | ")}{r.raw.length > 12 && " | …"}</td>
                </tr>
              ))}
              {rejected_rows.length === 0 && (
                <tr><td colSpan={3} className="empty">No rejected rows.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {run.columns && (
        <details className="card">
          <summary>{run.columns.length} columns in this file</summary>
          <p className="small ink-2 wrap-anywhere">{run.columns.join(" · ")}</p>
        </details>
      )}
    </div>
  );
}
