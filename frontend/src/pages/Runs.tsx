import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { NextDownload } from "../components/NextDownload";
import { useToast } from "../components/Toasts";
import { Pager, QueryState, RunStatusBadge, Segmented, SeverityBadge } from "../components/ui";
import {
  api, type RejectedRow, type RunDetail, type RunLot, type RunRowKind, type RunStatus,
} from "../lib/api";
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
  const { run, alerts } = q.data;
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
                <dd title="Missing from this file, but the auction (sale time + grace period) isn't over yet, or not scheduled">
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

      <RunRowsCard run={run} />

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

      {run.columns && (
        <details className="card">
          <summary>{run.columns.length} columns in this file</summary>
          <p className="small ink-2 wrap-anywhere">{run.columns.join(" · ")}</p>
        </details>
      )}
    </div>
  );
}

const KIND_LABEL: Record<RunRowKind, string> = {
  new: "New", updated: "Updated", removed: "Removed", rejected: "Rejected",
};
const isRejected = (row: RunLot | RejectedRow): row is RejectedRow => "line_no" in row;
const isLot = (row: RunLot | RejectedRow): row is RunLot => !("line_no" in row);

/** The lots a run created, changed or removed, and the rows it rejected — one page at a time:
 *  a single export can hold well over 100k of them. */
function RunRowsCard({ run }: { run: RunDetail["run"] }) {
  const navigate = useNavigate();
  const [kind, setKind] = useState<RunRowKind>("new");
  const [offset, setOffset] = useState(0);
  const q = useQuery({
    queryKey: ["run-rows", run.id, kind, offset],
    queryFn: () => api.runRows(run.id, { kind, limit: LIMIT, offset }),
  });
  const counts: Record<RunRowKind, number | null> = {
    new: run.rows_new, updated: run.rows_updated,
    removed: run.rows_removed, rejected: run.rows_rejected,
  };
  const lotUrl = (lot: RunLot) =>
    `/lots/${encodeURIComponent(lot.source)}/${encodeURIComponent(lot.lot_number)}`;
  // Only the first 1,000 rejected rows of a file are kept, so the tab can hold fewer than the count.
  const dropped = kind === "rejected" && q.data ? (run.rows_rejected ?? 0) - q.data.total : 0;

  return (
    <div className="card stack">
      <h2>What this run did</h2>
      <Segmented<RunRowKind> label="Which lots" value={kind}
        onChange={(v) => { setKind(v); setOffset(0); }}
        options={(Object.keys(KIND_LABEL) as RunRowKind[]).map((k) => ({
          value: k, label: `${KIND_LABEL[k]} (${int(counts[k])})`,
        }))} />
      {kind === "removed" && q.data?.available && q.data.reason && (
        <p className="muted small">Reason for removal: {q.data.reason}</p>
      )}
      {dropped > 0 && (
        <p className="muted small">
          Showing the {int(q.data?.total)} rejected rows that were kept; {int(dropped)} more were
          rejected but not stored.
        </p>
      )}
      <QueryState isLoading={q.isLoading} error={q.error} />
      {q.data && !q.data.available && (
        <p className="muted">The complete removed-lot list is unavailable for this older run.</p>
      )}
      {q.data && q.data.available && (
        <>
          <div className="table-wrap" style={{ opacity: q.isFetching ? 0.7 : 1 }}>
            {kind === "rejected" ? (
              <table>
                <thead><tr><th className="num">Line</th><th>Reason</th><th>Raw values</th></tr></thead>
                <tbody>
                  {q.data.items.filter(isRejected).map((r) => (
                    <tr key={r.line_no}>
                      <td className="num">{r.line_no}</td>
                      <td>{r.reason}</td>
                      <td className="mono small wrap-anywhere">
                        {r.raw.slice(0, 12).join(" | ")}{r.raw.length > 12 && " | …"}
                      </td>
                    </tr>
                  ))}
                  {q.data.items.length === 0 && (
                    <tr><td colSpan={3} className="empty">No rejected rows.</td></tr>
                  )}
                </tbody>
              </table>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Lot</th><th>Vehicle</th><th>VIN</th><th>Yard</th><th>Sale</th>
                    {kind === "removed" && <th>Removed</th>}
                  </tr>
                </thead>
                <tbody>
                  {q.data.items.filter(isLot).map((l) => (
                    <tr key={l.lot_number} className="clickable" onClick={() => navigate(lotUrl(l))}>
                      <td><Link to={lotUrl(l)}>{l.lot_number}</Link></td>
                      <td>{[l.year, l.make, l.model_group].filter(Boolean).join(" ") || "—"}
                        <div className="muted small">{l.trim}</div></td>
                      <td className="mono small">{l.vin ?? "—"}</td>
                      <td>{l.yard_name ?? "—"}</td>
                      <td>{dateTime(l.sale_at)}<div className="muted small">{l.sale_status}</div></td>
                      {kind === "removed" && <td>{dateTime(l.removed_at)}</td>}
                    </tr>
                  ))}
                  {q.data.items.length === 0 && (
                    <tr><td colSpan={kind === "removed" ? 6 : 5} className="empty">
                      No {KIND_LABEL[kind].toLowerCase()} lots.
                    </td></tr>
                  )}
                </tbody>
              </table>
            )}
          </div>
          <Pager offset={offset} limit={LIMIT} total={q.data.total} onChange={setOffset} />
        </>
      )}
    </div>
  );
}
