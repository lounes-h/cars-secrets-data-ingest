import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import {
  api, type AddedModel, type AIUsage, type AnalysisModel, type AnalysisImage, type AnalysisRequestRecord, type DamageRegion,
  type DamageSeverity, type LotAnalyses, type ObservedDamage, type SentContent, type SentRequest,
  type VehicleAnalysis,
} from "../lib/api";
import { bytes, dateTime, int } from "../lib/format";
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

const perMillion = (usd: number) => `$${usd}`;
const PROVIDERS: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI" };

/** Adds a model, or changes one a person added. Its id is offered from the provider's list,
 * which fills in its name; its price is typed, since neither provider's API publishes it. */
function ModelForm({ model, onDone }: { model: AnalysisModel | null; onDone: (saved?: string) => void }) {
  const qc = useQueryClient();
  const [fields, setFields] = useState({
    provider: model?.provider ?? "anthropic", id: model?.id ?? "", label: model?.label ?? "", note: model?.note ?? "",
    input_usd: String(model?.input_usd ?? ""), output_usd: String(model?.output_usd ?? ""),
  });
  const available = useQuery({
    queryKey: ["available-models", fields.provider],
    queryFn: () => api.availableModels(fields.provider),
    staleTime: 15 * 60_000,
    retry: false,
  });
  const save = useMutation({
    mutationFn: (body: AddedModel) => (model ? api.updateModel(model.id, body) : api.addModel(body)),
    onSuccess: (saved) => {
      void qc.invalidateQueries({ queryKey: ["analysis-models"] });
      onDone(saved.id);
    },
  });
  const set = (name: keyof typeof fields) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const value = e.target.value;
    setFields((f) => {
      const listed = name === "id" && !f.label ? available.data?.models.find((m) => m.id === value) : undefined;
      return { ...f, [name]: value, ...(listed && { label: listed.label }) };
    });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate({ ...fields, input_usd: Number(fields.input_usd), output_usd: Number(fields.output_usd) });
  };
  const title = model ? `Edit ${model.label}` : "Add a model";
  const provider = PROVIDERS[fields.provider];
  const count = available.data?.models.length;
  return (
    <form className="model-form" aria-label={title} onSubmit={submit}>
      <h3>{title}</h3>
      <label>
        Provider
        <select value={fields.provider} onChange={set("provider")}>
          {Object.entries(PROVIDERS).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
      </label>
      <label>
        Model id
        <input type="text" list="provider-models" required maxLength={100} pattern="[A-Za-z0-9][A-Za-z0-9._:\-]*"
          autoComplete="off" value={fields.id} onChange={set("id")} />
      </label>
      <datalist id="provider-models">
        {available.data?.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
      </datalist>
      <span className="muted small">
        {available.isLoading ? `Listing ${provider}'s models…`
          : available.error ? `${available.error.message}. Type the id.`
          : `${count} model${count === 1 ? "" : "s"} offered by ${provider}; any other id can be typed.`}
      </span>
      <label>Name<input type="text" required maxLength={100} value={fields.label} onChange={set("label")} /></label>
      <label>Note<input type="text" maxLength={300} value={fields.note} onChange={set("note")} /></label>
      <label>
        Input, $ per million tokens
        <input type="number" required min={0} step="any" value={fields.input_usd} onChange={set("input_usd")} />
      </label>
      <label>
        Output, $ per million tokens
        <input type="number" required min={0} step="any" value={fields.output_usd} onChange={set("output_usd")} />
      </label>
      {save.error && <span className="error" role="alert">{save.error.message}</span>}
      <div className="dialog-actions">
        <button type="button" onClick={() => onDone()}>Cancel</button>
        <button type="submit" className="primary" disabled={save.isPending}>Save</button>
      </div>
    </form>
  );
}

/** The lot page's action: generate, or regenerate once an analysis exists, with the model
 * chosen in a dialog, where models can also be added, and added ones changed or deleted. */
export function AnalysisButton({ source, lotNumber }: { source: string; lotNumber: string }) {
  const qc = useQueryClient();
  const key = ["lot-analysis", source, lotNumber];
  const dialog = useRef<HTMLDialogElement>(null);
  const analyses = useLotAnalyses(source, lotNumber);
  const models = useQuery({ queryKey: ["analysis-models"], queryFn: api.analysisModels, staleTime: Infinity });
  const [picked, setPicked] = useState<string | null>(null);
  const [editing, setEditing] = useState<AnalysisModel | "new" | null>(null);
  // a deleted or renamed model is no longer picked
  const model = models.data?.models.some((m) => m.id === picked) ? picked : models.data?.default;
  const generate = useMutation({
    mutationFn: (chosen: string) => api.requestAnalysis(source, lotNumber, chosen),
    // The queued request is current at once: the button never re-enables before the refetch.
    onSuccess: (queued) => qc.setQueryData<LotAnalyses>(key, (old) => ({ latest: old?.latest ?? null, current: queued })),
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteModel(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["analysis-models"] }),
  });
  const busy = generate.isPending || pending(analyses.data);
  const title = analyses.data?.latest ? "Regenerate AI Analysis" : "Generate AI Analysis";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    dialog.current?.close();
    generate.mutate(model!);
  };
  const open = () => {
    setEditing(null);
    dialog.current?.showModal();
  };
  const done = (saved?: string) => {
    if (saved) setPicked(saved);
    setEditing(null);
  };
  return (
    <>
      <button className="primary" disabled={busy || !analyses.data} onClick={open}>
        {busy ? "Generating AI Analysis…" : title}
      </button>
      {generate.error && <span className="error" role="alert">{generate.error.message}</span>}
      <dialog ref={dialog} className="model-dialog" aria-labelledby="model-dialog-title">
        <h2 id="model-dialog-title">{title}</h2>
        {editing ? <ModelForm model={editing === "new" ? null : editing} onDone={done} /> : (
          <form onSubmit={submit}>
            <p className="muted small">Choose the model. Each analysis is billed by the tokens it uses (photos count as input).</p>
            <QueryState isLoading={models.isLoading} error={models.error} />
            <fieldset>
              <legend className="visually-hidden">Model</legend>
              {models.data?.models.map((m) => (
                <div key={m.id} className="model-option">
                  <label>
                    <input type="radio" name="model" value={m.id} checked={model === m.id} onChange={() => setPicked(m.id)} />
                    <span>
                      <strong>{m.label}</strong>{m.id === models.data.default && <span className="muted small"> · default</span>}
                      <span className="muted small model-note">
                        {m.custom && `${PROVIDERS[m.provider] ?? m.provider} · `}{m.note && `${m.note} · `}
                        {perMillion(m.input_usd)} in / {perMillion(m.output_usd)} out per million tokens
                      </span>
                    </span>
                  </label>
                  {m.custom && (
                    <span className="model-actions">
                      <button type="button" className="small" aria-label={`Edit ${m.label}`} onClick={() => setEditing(m)}>Edit</button>
                      <button type="button" className="small" aria-label={`Delete ${m.label}`} disabled={remove.isPending}
                        onClick={() => remove.mutate(m.id)}>Delete</button>
                    </span>
                  )}
                </div>
              ))}
            </fieldset>
            {remove.error && <p className="error" role="alert">{remove.error.message}</p>}
            <div className="dialog-actions">
              <button type="button" className="small add-model" onClick={() => setEditing("new")}>Add a model</button>
              <button type="button" onClick={() => dialog.current?.close()}>Cancel</button>
              <button type="submit" className="primary" disabled={!model}>Generate</button>
            </div>
          </form>
        )}
      </dialog>
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

/** The message as sent: a photo's ID and the photo that follows it share a line. */
function SentMessage({ content }: { content: SentContent[] }) {
  const blocks: ReactNode[] = [];
  for (let i = 0; i < content.length; i++) {
    const block = content[i];
    const next = content[i + 1];
    if ("text" in block && next && "bytes" in next) {
      blocks.push(<div key={i} className="mono small">{block.text}: JPEG photo, {bytes(next.bytes)}</div>);
      i++;
    } else if ("text" in block) {
      blocks.push(<pre key={i} className="prompt">{block.text}</pre>);
    } else {
      blocks.push(<div key={i} className="mono small">JPEG photo, {bytes(block.bytes)}</div>);
    }
  }
  return <>{blocks}</>;
}

function SentCall({ title, sent }: { title: string; sent: SentRequest }) {
  // Anthropic's shape, or OpenAI's (see SentRequest)
  const { system, instructions, messages, input, output_config, text, ...params } = sent;
  const { format, ...output } = (output_config ?? text)!;
  const content = (messages ?? input)![0].content;
  const photos = content.filter((b) => "bytes" in b).length;
  return (
    <details className="sent-call">
      <summary>{title} · {sent.model} · {photos} photos</summary>
      <h5>Parameters</h5>
      <pre>{JSON.stringify({ ...params, [output_config ? "output_config" : "text"]: { ...output, format: format.type } }, null, 2)}</pre>
      <h5>System prompt</h5>
      <pre className="prompt">{system ?? instructions}</pre>
      <h5>User message</h5>
      <SentMessage content={content} />
      <details>
        <summary className="small">Output schema ({format.type})</summary>
        <pre>{JSON.stringify(format.schema, null, 2)}</pre>
      </details>
    </details>
  );
}

function RequestRecord({ request }: { request: AnalysisRequestRecord | null }) {
  if (!request) {
    return <p className="muted">Not recorded: this analysis was made before requests were kept. Regenerate it to see one.</p>;
  }
  return (
    <>
      <dl className="kv small">
        {Object.entries(request.settings).map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}><dt className="mono">{k}</dt><dd>{v}</dd></div>
        ))}
      </dl>
      {request.selection && <SentCall title="Photo selection call" sent={request.selection} />}
      {request.analysis && <SentCall title="Analysis call" sent={request.analysis} />}
    </>
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

      <h3>Prompt and Request</h3>
      <RequestRecord request={analysis.request} />

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
