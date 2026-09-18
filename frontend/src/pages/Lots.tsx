import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, QueryState, Segmented } from "../components/ui";
import { api } from "../lib/api";
import { dateTime, int, money } from "../lib/format";

const LIMIT = 50;
type State = "all" | "active" | "removed";

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function LotsPage() {
  const navigate = useNavigate();
  const [input, setInput] = useState("");
  const [state, setState] = useState<State>("all");
  const [offset, setOffset] = useState(0);
  const q = useDebounced(input.trim());
  const lots = useQuery({
    queryKey: ["lots", q, state, offset],
    queryFn: () => api.lots({ q, state, limit: LIMIT, offset }),
  });

  return (
    <div>
      <div className="topbar"><h1>Lots</h1></div>
      <div className="filters">
        <input type="search" placeholder="Lot number, VIN, make or model…" value={input}
          aria-label="Search lots" style={{ width: 320, maxWidth: "100%" }}
          onChange={(e) => { setInput(e.target.value); setOffset(0); }} />
        <Segmented<State> label="Lot state" value={state} onChange={(v) => { setState(v); setOffset(0); }}
          options={[{ value: "all", label: "All" }, { value: "active", label: "Active" }, { value: "removed", label: "Removed" }]} />
      </div>
      <QueryState isLoading={lots.isLoading} error={lots.error} />
      {lots.data && (
        <>
          <div className="table-wrap" style={{ opacity: lots.isFetching ? 0.7 : 1 }}>
            <table>
              <thead>
                <tr>
                  <th>Lot</th><th>Vehicle</th><th>VIN</th><th>Damage</th><th className="num">Odometer</th>
                  <th className="num">High bid</th><th>Yard</th><th>Sale</th><th>Last change</th><th>State</th>
                </tr>
              </thead>
              <tbody>
                {lots.data.items.map((l) => (
                  <tr key={`${l.source}/${l.lot_number}`} className="clickable"
                    onClick={() => navigate(`/lots/${l.source}/${l.lot_number}`)}>
                    <td><Link to={`/lots/${l.source}/${l.lot_number}`}>{l.lot_number}</Link>
                      <div className="muted small">{l.source}</div></td>
                    <td>{[l.year, l.make, l.model_group].filter(Boolean).join(" ")}
                      <div className="muted small">{l.trim}</div></td>
                    <td className="mono small">{l.vin ?? "—"}</td>
                    <td>{l.damage_primary ?? "—"}</td>
                    <td className="num">{l.odometer ? int(Number(l.odometer)) : "—"}</td>
                    <td className="num">{money(l.high_bid, l.currency)}</td>
                    <td>{l.yard_name ?? "—"}</td>
                    <td>{dateTime(l.sale_at)}</td>
                    <td>{dateTime(l.last_changed_at)}</td>
                    <td>{l.removed_at ? <Badge tone="neutral">removed</Badge> : <Badge tone="good">active</Badge>}</td>
                  </tr>
                ))}
                {lots.data.items.length === 0 && (
                  <tr><td colSpan={10} className="empty">No lots match.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <button className="small" disabled={offset === 0} onClick={() => setOffset(offset - LIMIT)}>Previous</button>
            <button className="small" disabled={lots.data.items.length < LIMIT}
              onClick={() => setOffset(offset + LIMIT)}>Next</button>
          </div>
        </>
      )}
    </div>
  );
}

export function LotDetailPage() {
  const { source = "", lotNumber = "" } = useParams();
  const q = useQuery({ queryKey: ["lot", source, lotNumber], queryFn: () => api.lot(source, lotNumber) });
  if (!q.data) return <QueryState isLoading={q.isLoading} error={q.error} />;
  const { lot, history } = q.data;
  const attributes = Object.entries(lot.attributes).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="stack">
      <div className="topbar">
        <h1>
          {[lot.year, lot.make, lot.model_group, lot.model_detail].filter(Boolean).join(" ")}{" "}
          <span className="muted">· lot {lot.lot_number}</span>
        </h1>
        {lot.removed_at ? <Badge tone="neutral">removed {dateTime(lot.removed_at)}</Badge> : <Badge tone="good">active</Badge>}
      </div>
      <div className="grid two-col">
        <div className="card">
          <h2>Summary</h2>
          <dl className="kv">
            <dt>Provider</dt><dd>{lot.source}</dd>
            <dt>VIN</dt><dd className="mono">{lot.vin ?? "—"}</dd>
            <dt>Damage</dt><dd>{[lot.damage_primary, lot.damage_secondary].filter(Boolean).join(" / ") || "—"}</dd>
            <dt>Odometer</dt><dd>{lot.odometer ? int(Number(lot.odometer)) : "—"}</dd>
            <dt>Retail value</dt><dd>{money(lot.est_retail_value, lot.currency)}</dd>
            <dt>High bid</dt><dd>{money(lot.high_bid, lot.currency)}</dd>
            <dt>Buy it now</dt><dd>{money(lot.buy_it_now_price, lot.currency)}</dd>
            <dt>Sale</dt><dd>{dateTime(lot.sale_at)} · {lot.sale_status ?? "—"}</dd>
            <dt>Yard</dt><dd>{lot.yard_name ?? "—"} ({lot.location_city}, {lot.location_state})</dd>
            <dt>First seen</dt><dd>{dateTime(lot.first_seen_at)} · <Link to={`/runs/${lot.first_file_id}`}>run #{lot.first_file_id}</Link></dd>
            <dt>Last seen</dt><dd>{dateTime(lot.last_seen_at)} · <Link to={`/runs/${lot.last_file_id}`}>run #{lot.last_file_id}</Link></dd>
          </dl>
        </div>
        <div className="card">
          <h2>Change history</h2>
          {history.length === 0 && <div className="muted">Unchanged since first seen.</div>}
          {history.map((h, i) => (
            <div key={i} style={{ marginBottom: 12 }}>
              <div className="small ink-2">
                {dateTime(h.changed_at)}
                {h.file_id && <> · <Link to={`/runs/${h.file_id}`}>run #{h.file_id}</Link></>}
              </div>
              <table>
                <tbody>
                  {h.changes.map((c) => (
                    <tr key={c.field}>
                      <td className="mono small">{c.field}</td>
                      <td className="small"><span className="diff-old">{c.old ?? "∅"}</span> → {c.new ?? "∅"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      </div>
      <details className="card">
        <summary>All {attributes.length} source values</summary>
        <dl className="kv" style={{ marginTop: 10 }}>
          {attributes.map(([k, v]) => (
            <div key={k} style={{ display: "contents" }}><dt className="mono small">{k}</dt><dd>{v}</dd></div>
          ))}
        </dl>
      </details>
    </div>
  );
}
