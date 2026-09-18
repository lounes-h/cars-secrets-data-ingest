import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Badge, QueryState } from "../components/ui";
import { api, type Column } from "../lib/api";
import { dateTime } from "../lib/format";

export function SchemaPage() {
  const q = useQuery({ queryKey: ["schema"], queryFn: () => api.schema() });
  if (!q.data) return <QueryState isLoading={q.isLoading} error={q.error} />;

  const bySource = new Map<string, Column[]>();
  for (const c of q.data.items) bySource.set(c.source, [...(bySource.get(c.source) ?? []), c]);

  return (
    <div className="stack">
      <div className="topbar"><h1>Schema</h1></div>
      <p className="ink-2" style={{ margin: 0 }}>
        Every column a provider has ever sent. All values are stored per lot, so a new or renamed
        column never breaks ingestion. <em>Mapped to</em> shows the typed column it feeds; unmapped
        columns stay queryable in <code>lots.attributes</code>.
      </p>
      {[...bySource.entries()].map(([source, cols]) => {
        const missing = cols.filter((c) => c.status === "missing").length;
        const unmapped = cols.filter((c) => !c.mapped_to).length;
        return (
          <div key={source}>
            <h2>
              {source} <span className="muted">· {cols.length} columns · {unmapped} unmapped
                {missing > 0 && ` · ${missing} missing from the latest file`}</span>
            </h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Header in file</th><th>Key</th><th>Mapped to</th><th>Status</th>
                    <th>First seen</th><th>Last seen</th></tr>
                </thead>
                <tbody>
                  {cols.map((c) => (
                    <tr key={c.normalized_name}>
                      <td>{c.source_name}</td>
                      <td className="mono">{c.normalized_name}</td>
                      <td className="mono">{c.mapped_to ?? <span className="muted">attributes only</span>}</td>
                      <td>
                        {c.status === "active"
                          ? <Badge tone="good">present</Badge>
                          : <Badge tone="warning">missing</Badge>}
                      </td>
                      <td>{dateTime(c.first_seen_at)}
                        {c.first_file_id && <> · <Link to={`/runs/${c.first_file_id}`}>#{c.first_file_id}</Link></>}</td>
                      <td>{dateTime(c.last_seen_at)}
                        {c.last_file_id && <> · <Link to={`/runs/${c.last_file_id}`}>#{c.last_file_id}</Link></>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
      {bySource.size === 0 && <div className="card empty">No file ingested yet.</div>}
    </div>
  );
}
