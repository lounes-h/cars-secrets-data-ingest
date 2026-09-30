import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { AnalysisButton, AnalysisSection } from "../components/Analysis";
import { FavoriteButton } from "../components/Favorite";
import { Gallery } from "../components/Gallery";
import { activeCount, LotFilters, type Filters } from "../components/LotFilters";
import { Badge, Pager, QueryState, Segmented } from "../components/ui";
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
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview });
  const counts = overview.data?.lots;
  const [input, setInput] = useState("");
  const [state, setState] = useState<State>("all");
  const [offset, setOffset] = useState(0);
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [filters, setFilters] = useState<Filters>({});
  const [showFilters, setShowFilters] = useState(false);
  const q = useDebounced(input.trim());
  const applied = useDebounced(filters);
  const active = activeCount(filters);
  const lots = useQuery({
    queryKey: ["lots", q, state, offset, favoritesOnly, applied],
    queryFn: () => api.lots({
      q, state, limit: LIMIT, offset, ...(favoritesOnly && { favorites: "true" }), ...applied,
    }),
  });

  return (
    <div>
      <div className="topbar"><h1>Lots</h1></div>
      <div className="filters">
        <input type="search" placeholder="Lot number, VIN, make or model…" value={input}
          aria-label="Search lots" style={{ width: 320, maxWidth: "100%" }}
          onChange={(e) => { setInput(e.target.value); setOffset(0); }} />
        <Segmented<State> label="Lot state" value={state} onChange={(v) => { setState(v); setOffset(0); }}
          options={[
            { value: "all", label: `All (${int(counts ? counts.active + counts.removed : null)})` },
            { value: "active", label: `Active (${int(counts?.active)})` },
            { value: "removed", label: `Removed (${int(counts?.removed)})` },
          ]} />
        {/* A switch: while on, the list and every filter cover only favorites, so it must show. */}
        <button className="toggle" aria-pressed={favoritesOnly}
          onClick={() => { setFavoritesOnly(!favoritesOnly); setOffset(0); }}>
          {favoritesOnly ? "★ Favorites only" : "☆ Favorites"}
        </button>
        <button aria-expanded={showFilters} onClick={() => setShowFilters(!showFilters)}>
          Filters{active ? ` (${active})` : ""}
        </button>
        {active > 0 && (
          <button className="small" onClick={() => { setFilters({}); setOffset(0); }}>Clear filters</button>
        )}
        {lots.data && (
          <span className="muted small match-count">
            {int(lots.data.total)} {favoritesOnly && "favorite "}{lots.data.total === 1 ? "lot" : "lots"}
          </span>
        )}
      </div>
      {showFilters && <LotFilters value={filters} onChange={(f) => { setFilters(f); setOffset(0); }} />}
      <QueryState isLoading={lots.isLoading} error={lots.error} />
      {lots.data && (
        <>
          <div className="table-wrap" style={{ opacity: lots.isFetching ? 0.7 : 1 }}>
            <table>
              <thead>
                <tr>
                  <th><span className="visually-hidden">Favorite</span></th>
                  <th><span className="visually-hidden">Photo</span></th><th>Lot</th><th>Vehicle</th><th>VIN</th><th>Damage</th><th className="num">Odometer</th>
                  <th className="num">High bid</th><th>Yard</th><th>Sale</th><th>Last change</th><th>State</th>
                </tr>
              </thead>
              <tbody>
                {lots.data.items.map((l) => (
                  <tr key={`${l.source}/${l.lot_number}`} className="clickable"
                    onClick={() => navigate(`/lots/${l.source}/${l.lot_number}`)}>
                    <td><FavoriteButton source={l.source} lotNumber={l.lot_number} favorite={l.favorite} /></td>
                    <td>{l.thumbnail_url
                      ? <img className="lot-thumb" src={l.thumbnail_url} alt="" loading="lazy" referrerPolicy="no-referrer" />
                      : <div className="lot-thumb" />}</td>
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
                  <tr><td colSpan={12} className="empty">
                    {favoritesOnly ? "No favorites match. Mark lots with ☆ to find them here." : "No lots match."}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} limit={LIMIT} total={lots.data.total} onChange={setOffset} />
        </>
      )}
    </div>
  );
}

export function LotDetailPage() {
  const { source = "", lotNumber = "" } = useParams();
  const q = useQuery({ queryKey: ["lot", source, lotNumber], queryFn: () => api.lot(source, lotNumber) });
  const photos = useQuery({
    queryKey: ["lot-images", source, lotNumber],
    queryFn: () => api.lotImages(source, lotNumber),
    staleTime: 15 * 60_000,
  });
  const back = <Link to="/lots" className="back-link">← Lots</Link>;
  if (!q.data) return <div className="stack">{back}<QueryState isLoading={q.isLoading} error={q.error} /></div>;
  const { lot, history } = q.data;
  const attributes = Object.entries(lot.attributes).sort(([a], [b]) => a.localeCompare(b));
  const title = [lot.year, lot.make, lot.model_group, lot.model_detail].filter(Boolean).join(" ");

  return (
    <div className="stack">
      {back}
      <div className="topbar">
        <h1>
          {title}{" "}
          <span className="muted">· lot {lot.lot_number}</span>
        </h1>
        <div className="lot-actions">
          <FavoriteButton source={lot.source} lotNumber={lot.lot_number} favorite={lot.favorite} labelled />
          {lot.removed_at ? <Badge tone="neutral">removed {dateTime(lot.removed_at)}</Badge> : <Badge tone="good">active</Badge>}
          {lot.listing_url && (
            <a className="button-link" href={lot.listing_url} target="_blank" rel="noopener noreferrer">
              View on {lot.source === "copart" ? "Copart" : lot.source} ↗
            </a>
          )}
          <AnalysisButton source={source} lotNumber={lotNumber} />
        </div>
      </div>
      <div className="grid two-col">
        <div className="card">
          {photos.isLoading
            ? <div className="gallery-empty muted">Loading photos…</div>
            : <Gallery images={photos.data?.images ?? []} alt={title}
                complete={photos.data?.complete ?? false} moreUrl={lot.listing_url} />}
        </div>
        <div className="card">
          <h2>Summary</h2>
          <dl className="kv">
            <dt>Provider</dt><dd>{lot.source}</dd>
            <dt>VIN</dt><dd className="mono">{lot.vin ?? "—"}</dd>
            <dt>Damage</dt><dd>{[lot.damage_primary, lot.damage_secondary].filter(Boolean).join(" / ") || "—"}</dd>
            <dt>Odometer</dt><dd>{lot.odometer ? int(Number(lot.odometer)) : "—"}</dd>
            <dt>Retail value</dt><dd>{money(lot.est_retail_value, lot.currency)}</dd>
            <dt>High bid</dt><dd>{money(lot.high_bid, lot.currency)}</dd>
            <dt>Predicted price</dt><dd>{money(lot.predicted_final_price, lot.currency)}</dd>
            <dt>Buy it now</dt><dd>{money(lot.buy_it_now_price, lot.currency)}</dd>
            <dt>Sale</dt><dd>{dateTime(lot.sale_at)} · {lot.sale_status ?? "—"}</dd>
            <dt>Yard</dt><dd>{lot.yard_name ?? "—"} ({lot.location_city}, {lot.location_state})</dd>
            <dt>First seen</dt><dd>{dateTime(lot.first_seen_at)} · <Link to={`/runs/${lot.first_file_id}`}>run #{lot.first_file_id}</Link></dd>
            <dt>Last seen</dt><dd>{dateTime(lot.last_seen_at)} · <Link to={`/runs/${lot.last_file_id}`}>run #{lot.last_file_id}</Link></dd>
          </dl>
        </div>
      </div>
      <AnalysisSection source={source} lotNumber={lotNumber} />
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
