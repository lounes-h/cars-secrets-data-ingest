import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Pager, QueryState, Segmented, SeverityBadge } from "../components/ui";
import { api, type Alert, type Severity } from "../lib/api";
import { ago, dateTime } from "../lib/format";

const LIMIT = 50;
type State = "open" | "resolved" | "all";

function AlertRow({ alert }: { alert: Alert }) {
  const qc = useQueryClient();
  const done = () => {
    qc.invalidateQueries({ queryKey: ["alerts"] });
    qc.invalidateQueries({ queryKey: ["overview"] });
  };
  const ack = useMutation({ mutationFn: () => api.ackAlert(alert.id), onSuccess: done });
  const resolve = useMutation({ mutationFn: () => api.resolveAlert(alert.id), onSuccess: done });
  const open = !alert.resolved_at;
  const hasDetails = Object.keys(alert.details ?? {}).length > 0;

  return (
    <div className={`alert-row ${open && alert.severity === "critical" ? "critical" : ""}`}>
      <SeverityBadge severity={alert.severity} />
      <div style={{ minWidth: 0 }}>
        <div className="alert-title">{alert.title}</div>
        <div className="alert-meta">
          {alert.type}{alert.source && ` · ${alert.source}`}
          {" · first "}{dateTime(alert.created_at)}
          {alert.occurrences > 1 && ` · seen ${alert.occurrences}× (last ${ago(alert.last_seen_at)})`}
          {alert.file_id && <> · <Link to={`/runs/${alert.file_id}`}>run #{alert.file_id}</Link></>}
          {alert.acked_at && ` · acknowledged by ${alert.acked_by}`}
          {alert.resolved_at && ` · resolved ${ago(alert.resolved_at)} by ${alert.resolved_by}`}
        </div>
        {hasDetails && (
          <details>
            <summary>Details</summary>
            <pre>{JSON.stringify(alert.details, null, 2)}</pre>
          </details>
        )}
      </div>
      {open && (
        <div className="alert-actions">
          {!alert.acked_at && (
            <button className="small" disabled={ack.isPending} onClick={() => ack.mutate()}>Acknowledge</button>
          )}
          <button className="small" disabled={resolve.isPending} onClick={() => resolve.mutate()}>Resolve</button>
        </div>
      )}
    </div>
  );
}

export function AlertsPage() {
  const [state, setState] = useState<State>("open");
  const [severity, setSeverity] = useState<"" | Severity>("");
  const [offset, setOffset] = useState(0);
  const q = useQuery({
    queryKey: ["alerts", state, severity, offset],
    queryFn: () => api.alerts({ state, severity, limit: LIMIT, offset }),
  });

  return (
    <div>
      <div className="topbar"><h1>Alerts</h1></div>
      <div className="filters">
        <Segmented<State> label="State" value={state} onChange={(v) => { setState(v); setOffset(0); }}
          options={[{ value: "open", label: "Open" }, { value: "resolved", label: "Resolved" }, { value: "all", label: "All" }]} />
        <Segmented<"" | Severity> label="Severity" value={severity}
          onChange={(v) => { setSeverity(v); setOffset(0); }}
          options={[{ value: "", label: "Any severity" }, { value: "critical", label: "Critical" },
            { value: "warning", label: "Warning" }, { value: "info", label: "Info" }]} />
      </div>
      <p className="small muted" style={{ marginTop: 0 }}>
        Conditions like a silent worker or a missing file resolve themselves once they clear.
        A repeat of an open alert increases its count instead of adding a row.
      </p>
      <QueryState isLoading={q.isLoading} error={q.error} />
      {q.data && (
        <>
          <div className="card" style={{ padding: 0 }}>
            {q.data.items.map((a) => <AlertRow key={a.id} alert={a} />)}
            {q.data.items.length === 0 && <div className="empty">No alerts here.</div>}
          </div>
          <Pager offset={offset} limit={LIMIT} total={q.data.total} onChange={setOffset} />
        </>
      )}
    </div>
  );
}
