import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  api, type AIUsage, type AnalysisImage, type DamageRegion, type DamageSeverity, type LotAnalyses,
  type ObservedDamage, type VehicleAnalysis,
} from "../lib/api";
import { dateTime, int } from "../lib/format";
import { Badge, QueryState } from "./ui";

// The AI condition and damage analysis of a lot: generated on request by the worker, shown on
// the lot page. Photos are the auction's own; the analysis only adds marked areas over them.

const pending = (data?: LotAnalyses) =>
  data?.current?.status === "queued" || data?.current?.status === "processing";

function useLotAnalyses(source: string, lotNumber: string) {
  return useQuery({
    queryKey: ["lot-analysis", source, lotNumber],
    queryFn: () => api.lotAnalyses(source, lotNumber),
    // Live events refresh it; polling only covers a dropped event stream while one runs.
    refetchInterval: (query) => (pending(query.state.data) ? 5000 : false),
  });
}

/** "left_front_fender" -> "Left front fender" */
export function words(snake: string): string {
  const text = snake.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const percent = (confidence: number) => `${Math.round(confidence * 100)}%`;

const usd = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 3,
});
const STEP_NAMES: Record<string, string> = { selection: "photo selection", analysis: "analysis" };

/** "14,210 tokens (11,000 in, 3,210 out) · $0.108 (photo selection $0.021, analysis $0.087)" */
export function spent(usage: VehicleAnalysis["usage"]): string | null {
  const steps = Object.entries(usage ?? {}).filter((e): e is [string, AIUsage] => !!e[1]);
  if (steps.length === 0) return null;
  const input = steps.reduce((n, [, u]) => n + u.input_tokens, 0);
  const output = steps.reduce((n, [, u]) => n + u.output_tokens, 0);
  const known = steps.every(([, u]) => u.cost_usd != null);
  const cost = known ? usd.format(steps.reduce((n, [, u]) => n + u.cost_usd!, 0)) : "cost unknown";
  const parts = known && steps.length > 1
    ? ` (${steps.map(([step, u]) => `${STEP_NAMES[step]} ${usd.format(u.cost_usd!)}`).join(", ")})`
    : "";
  return `${int(input + output)} tokens (${int(input)} in, ${int(output)} out) · ${cost}${parts}`;
}

const SEVERITY_TONE = { severe: "critical", moderate: "warning", minor: "info", unknown: "neutral" } as const;

function SeverityTag({ severity }: { severity: DamageSeverity }) {
  return <Badge tone={SEVERITY_TONE[severity]}>{severity}</Badge>;
}

/** The lot page's action: generate, or regenerate once an analysis exists. */
export function AnalysisButton({ source, lotNumber }: { source: string; lotNumber: string }) {
  const qc = useQueryClient();
  const key = ["lot-analysis", source, lotNumber];
  const analyses = useLotAnalyses(source, lotNumber);
  const generate = useMutation({
    mutationFn: () => api.requestAnalysis(source, lotNumber),
    // The queued request is current at once: the button never re-enables before the refetch.
    onSuccess: (queued) => qc.setQueryData<LotAnalyses>(key, (old) => ({ latest: old?.latest ?? null, current: queued })),
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });
  const busy = generate.isPending || pending(analyses.data);
  return (
    <>
      <button className="primary" disabled={busy || !analyses.data} onClick={() => generate.mutate()}>
        {busy ? "Generating AI Analysis…" : analyses.data?.latest ? "Regenerate AI Analysis" : "Generate AI Analysis"}
      </button>
      {generate.error && <span className="error" role="alert">{generate.error.message}</span>}
    </>
  );
}

function Progress({ current, latest }: LotAnalyses) {
  if (!current) return null;
  if (current.status === "queued" || current.status === "processing") {
    return (
      <div className="banner" role="status">
        <Badge tone="info">{current.status === "queued" ? "queued" : "generating"}</Badge>
        <span>Analyzing the photos; this takes a minute or two.{latest && " The previous analysis is shown until then."}</span>
      </div>
    );
  }
  if (current.status === "failed" && (!latest || current.id > latest.id)) {
    return (
      <div className="banner degraded" role="alert">
        <Badge tone="warning">failed</Badge>
        <span className="wrap-anywhere">
          The latest analysis failed ({dateTime(current.completed_at)}): {current.error}
          {spent(current.usage) && ` It used ${spent(current.usage)}.`}
          {latest && " The previous analysis is shown."}
        </span>
      </div>
    );
  }
  return null;
}

function RegionDetails({ region, damage }: { region: DamageRegion; damage: ObservedDamage }) {
  return (
    <div className="region-details">
      <strong>{region.label}</strong>
      <div className="status-line small">
        {words(region.part_name)}
        <SeverityTag severity={damage.severity} />
        <span className="muted">{percent(region.confidence)} confidence</span>
      </div>
      <div>{region.explanation}</div>
    </div>
  );
}

/** An original auction photo with the damaged areas the analysis marked on it. */
function DamagePhoto({ image, regions, damage }: {
  image: AnalysisImage; regions: DamageRegion[]; damage: Map<string, ObservedDamage>;
}) {
  const [active, setActive] = useState<string | null>(null);
  const shown = regions.find((r) => r.region_id === active);
  return (
    <figure className="damage-photo">
      <div className="damage-frame">
        <img src={image.full ?? image.hd ?? image.thumb ?? ""} loading="lazy" referrerPolicy="no-referrer"
          alt={`Photo ${image.position} of the listing (${image.image_id})`} />
        {regions.map((r) => {
          const box = r.bounding_box;
          return (
            <button key={r.region_id} className="damage-box" aria-pressed={r.region_id === active}
              aria-label={`${r.label}, ${percent(r.confidence)} confidence`}
              style={{ left: `${box.x * 100}%`, top: `${box.y * 100}%`,
                width: `${box.width * 100}%`, height: `${box.height * 100}%` }}
              onMouseEnter={() => setActive(r.region_id)} onFocus={() => setActive(r.region_id)}
              onClick={() => setActive(r.region_id)} />
          );
        })}
      </div>
      <figcaption>
        <div className="muted small">Photo {image.position} · {image.image_id}</div>
        {shown
          ? <RegionDetails region={shown} damage={damage.get(shown.damage_id)!} />
          : <div className="muted small">
              {regions.length ? "Point at or select a marked area for details." : "No damage marked on this photo."}
            </div>}
      </figcaption>
    </figure>
  );
}

function List({ items, empty }: { items: string[]; empty: string }) {
  return items.length ? <ul>{items.map((t, i) => <li key={i}>{t}</li>)}</ul> : <p className="muted">{empty}</p>;
}

function Result({ analysis }: { analysis: VehicleAnalysis }) {
  const result = analysis.result!;
  const { general_summary: general, damage_analysis: damage } = result;
  const observed = new Map(damage.observed_damage.map((d) => [d.damage_id, d]));
  const regions = new Map(result.image_annotations.map((a) => [a.image_id, a.regions]));
  // The server checked every reference: each ID below names a supplied photo or observed damage.
  const images = analysis.images!;
  const photo = new Map(images.map((i) => [i.image_id, i.position]));
  const photos = (ids: string[]) => ids.map((id) => `#${photo.get(id)}`).join(", ");

  return (
    <div className="analysis">
      <p className="muted small">
        {analysis.provider} {analysis.model} · prompt {analysis.prompt_version} · schema {analysis.schema_version}
        {" · "}{analysis.image_count} photos · {dateTime(analysis.completed_at)}
        {analysis.requested_by && ` · requested by ${analysis.requested_by}`}
        {spent(analysis.usage) && <><br />Used {spent(analysis.usage)}</>}
      </p>

      <h3>General Summary</h3>
      <p>{general.summary}</p>
      <div className="grid two-col">
        <div><h4>Key findings</h4><List items={general.key_findings} empty="None." /></div>
        <div><h4>Inspect first</h4><List items={general.inspection_priorities} empty="None." /></div>
      </div>

      <h3>Damage Analysis</h3>
      <p>{damage.summary}</p>

      <h4>Observed Damage</h4>
      {damage.observed_damage.length === 0 ? <p className="muted">No damage visible in the photos.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Part</th><th>Location</th><th>Severity</th><th>Description</th><th className="num">Confidence</th><th>Photos</th></tr></thead>
            <tbody>
              {damage.observed_damage.map((d) => (
                <tr key={d.damage_id}>
                  <td>{words(d.part_name)}</td><td>{words(d.location)}</td>
                  <td><SeverityTag severity={d.severity} /></td><td>{d.description}</td>
                  <td className="num">{percent(d.confidence)}</td><td>{photos(d.evidence_image_ids)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4>Possible Hidden Damage</h4>
      <p className="muted small">Inferred from the visible damage, not confirmed: a physical inspection is recommended.</p>
      {damage.possible_hidden_damage.length === 0 ? <p className="muted">None suspected.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Part</th><th>Possible damage</th><th>Why</th><th>Based on</th><th className="num">Confidence</th></tr></thead>
            <tbody>
              {damage.possible_hidden_damage.map((h) => (
                <tr key={h.damage_id}>
                  <td>{words(h.part_name)}</td><td>{h.description}</td><td>{h.reason}</td>
                  <td>{h.based_on_observed_damage_ids.map((id) => words(observed.get(id)!.part_name)).join(", ")}</td>
                  <td className="num">{percent(h.confidence)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>Damaged Parts</h3>
      <p className="muted small">Suggested actions are a visual assessment, not repair instructions.</p>
      {result.damaged_parts.length === 0 ? <p className="muted">None.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Part</th><th>Severity</th><th>Suggested action</th><th className="num">Confidence</th></tr></thead>
            <tbody>
              {result.damaged_parts.map((p) => (
                <tr key={p.part_name}>
                  <td>{words(p.part_name)}</td><td><SeverityTag severity={p.severity} /></td>
                  <td>{words(p.suggested_action)}</td><td className="num">{percent(p.confidence)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>Interactive Damage Images</h3>
      <div className="damage-photos">
        {images.map((image) => (
          <DamagePhoto key={image.image_id} image={image} regions={regions.get(image.image_id) ?? []}
            damage={observed} />
        ))}
      </div>

      <h3>Limitations</h3>
      <List items={result.limitations} empty="None stated." />

      <details>
        <summary className="small">Raw analysis JSON</summary>
        <pre className="raw-json">{JSON.stringify(result, null, 2)}</pre>
      </details>
    </div>
  );
}

/** The lot page's AI Analysis section: the latest completed analysis, and any newer attempt. */
export function AnalysisSection({ source, lotNumber }: { source: string; lotNumber: string }) {
  const analyses = useLotAnalyses(source, lotNumber);
  return (
    <section className="card stack" aria-labelledby="ai-analysis">
      <h2 id="ai-analysis">AI Analysis</h2>
      <QueryState isLoading={analyses.isLoading} error={analyses.error} />
      {analyses.data && <Progress {...analyses.data} />}
      {analyses.data?.latest
        ? <Result analysis={analyses.data.latest} />
        : analyses.data && !pending(analyses.data) && (
          <p className="muted">
            No analysis yet. Generate one to get a summary of the vehicle's condition, its visible and
            possible hidden damage, and the damaged areas marked on its photos.
          </p>
        )}
    </section>
  );
}
