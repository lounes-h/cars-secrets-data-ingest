// Thin typed client for the dashboard API. The session is an http-only cookie, so requests
// just need to be same-origin; nothing auth-related is ever stored in JS.

export type Severity = "info" | "warning" | "critical";
export type RunStatus = "processing" | "succeeded" | "failed" | "duplicate" | "interrupted";

export interface Run {
  id: number;
  source: string;
  filename: string;
  sha256: string;
  size_bytes: number | null;
  status: RunStatus;
  received_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  rows_total: number | null;
  rows_new: number | null;
  rows_updated: number | null;
  rows_unchanged: number | null;
  rows_rejected: number | null;
  rows_removed: number | null;
  rows_removal_deferred: number | null;
  removal_skipped: string[];
  error: string | null;
}

export interface RunDetail {
  run: Run & {
    columns: string[] | null;
    field_errors: Record<string, number> | null;
    archived_path: string | null;
  };
  alerts: Pick<Alert, "id" | "severity" | "type" | "title" | "created_at" | "resolved_at">[];
}

/** The four lists a run produced; each is paged separately, they can hold 100k+ rows. */
export type RunRowKind = "new" | "updated" | "removed" | "rejected";

export interface RejectedRow {
  line_no: number;
  reason: string;
  raw: string[];
}

export interface RunRows {
  items: (RunLot | RejectedRow)[];
  total: number;
  /** False only when an older run's removed lots cannot be reconstructed. */
  available: boolean;
  /** Why these lots were removed; null for the other kinds (rejections say it per row). */
  reason: string | null;
}

export interface Alert {
  id: number;
  severity: Severity;
  type: string;
  title: string;
  details: Record<string, unknown>;
  source: string | null;
  fingerprint: string;
  occurrences: number;
  file_id: number | null;
  created_at: string;
  last_seen_at: string;
  acked_at: string | null;
  acked_by: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
}

export interface ProviderStatus {
  source: string;
  display_name: string;
  last_run: Pick<
    Run,
    "id" | "filename" | "status" | "received_at" | "finished_at" | "rows_total" | "rows_new"
    | "rows_updated" | "rows_removed" | "rows_rejected"
  > | null;
  last_success_at: string | null;
  lots_active: number;
  stale_after_minutes: number;
}

export interface Overview {
  health: "healthy" | "degraded" | "down";
  worker: {
    worker_id: string;
    state: string;
    started_at: string;
    last_beat_at: string;
    healthy: boolean;
  } | null;
  providers: ProviderStatus[];
  lots: { active: number; removed: number; new_24h: number; removed_24h: number; changed_24h: number };
  downloader: Downloader;
  alerts_open: Record<Severity, number>;
  now: string;
}

export interface Column {
  source: string;
  normalized_name: string;
  source_name: string;
  mapped_to: string | null;
  status: "active" | "missing";
  first_seen_at: string;
  last_seen_at: string;
  first_file_id: number | null;
  last_file_id: number | null;
}

export interface Lot {
  source: string;
  lot_number: string;
  year: number | null;
  make: string | null;
  model_group: string | null;
  model_detail: string | null;
  trim: string | null;
  vin: string | null;
  damage_primary: string | null;
  damage_secondary: string | null;
  odometer: string | null;
  est_retail_value: string | null;
  repair_cost: string | null;
  high_bid: string | null;
  buy_it_now_price: string | null;
  currency: string | null;
  sale_status: string | null;
  sale_at: string | null;
  yard_code: string | null;
  yard_name: string | null;
  location_city: string | null;
  location_state: string | null;
  thumbnail_url: string | null;
  first_seen_at: string;
  last_seen_at: string;
  last_changed_at: string;
  removed_at: string | null;
}

/** Values the Lots page's filters offer: those of the active lots. */
export interface LotFilterOptions {
  makes: string[];
  damages: string[];
  states: string[];
  yards: string[];
  keys: string[];
  runs: string[];
}

export type RunLot = Pick<
  Lot,
  "source" | "lot_number" | "year" | "make" | "model_group" | "trim" | "vin" | "sale_status"
  | "sale_at" | "yard_name" | "removed_at"
>;

export interface LotDetail {
  lot: Lot & {
    attributes: Record<string, string>;
    first_file_id: number;
    last_file_id: number;
    /** The lot's page on the auction site (all photos, bidding). */
    listing_url: string | null;
  };
  history: {
    changed_at: string;
    file_id: number | null;
    changes: { field: string; old: string | null; new: string | null }[];
  }[];
}

export interface LotImage {
  thumb: string | null;
  full: string | null;
  hd: string | null;
}

/** AI condition and damage analysis of a lot (schema 1.0), run by the worker on request. */
export type AnalysisStatus = "queued" | "processing" | "completed" | "failed";
export type DamageSeverity = "minor" | "moderate" | "severe" | "unknown";

export interface ObservedDamage {
  damage_id: string;
  part_name: string;
  location: string;
  severity: DamageSeverity;
  description: string;
  confidence: number;
  evidence_image_ids: string[];
}

export interface PossibleHiddenDamage {
  damage_id: string;
  part_name: string;
  description: string;
  reason: string;
  based_on_observed_damage_ids: string[];
  confidence: number;
  inspection_recommended: boolean;
}

export interface DamagedPart {
  part_name: string;
  severity: DamageSeverity;
  suggested_action: "inspect" | "repair" | "replace" | "repair_or_replace" | "unknown";
  confidence: number;
  observed_damage_ids: string[];
}

export interface DamageRegion {
  region_id: string;
  damage_id: string;
  part_name: string;
  label: string;
  explanation: string;
  confidence: number;
  /** Fractions of the photo's width and height, from its top-left corner. */
  bounding_box: { x: number; y: number; width: number; height: number };
}

export interface AnalysisResult {
  schema_version: string;
  language: string;
  general_summary: { summary: string; key_findings: string[]; inspection_priorities: string[] };
  damage_analysis: {
    summary: string;
    observed_damage: ObservedDamage[];
    possible_hidden_damage: PossibleHiddenDamage[];
  };
  damaged_parts: DamagedPart[];
  image_annotations: { image_id: string; regions: DamageRegion[] }[];
  limitations: string[];
}

/** A photo sent to the model: `image_id` is how the analysis refers to it. */
export interface AnalysisImage extends LotImage {
  image_id: string;
  /** 1-based position in the lot's photo list. */
  position: number;
}

/** Tokens and cost of one model call. No `cost_usd` for a model without a known price (null)
 * or an analysis made before costs were recorded (absent). */
export interface AIUsage {
  model: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd?: number | null;
}

/** One block of a message as sent to the model; photos are recorded by their size. */
export type SentContent =
  | { type: "text"; text: string }
  | { type: "image"; media_type: string; bytes: number };

/** A model call's request as sent: parameters, system prompt, message, output schema. */
export interface SentRequest {
  model: string;
  system: string;
  output_config: { format: { type: string; schema: unknown }; [key: string]: unknown };
  messages: { role: string; content: SentContent[] }[];
  [key: string]: unknown;
}

/** What shaped an analysis: its settings, and each model call's request. */
export interface AnalysisRequestRecord {
  settings: Record<string, string | number>;
  selection?: SentRequest;
  analysis?: SentRequest;
}

export interface VehicleAnalysis {
  id: number;
  source: string;
  lot_number: string;
  status: AnalysisStatus;
  language: string;
  requested_by: string | null;
  provider: string | null;
  model: string | null;
  prompt_version: string | null;
  schema_version: string | null;
  images: AnalysisImage[] | null;
  image_count: number | null;
  result: AnalysisResult | null;
  /** Per model call: choosing the photos (only for lots with many), then the analysis. */
  usage: { selection?: AIUsage | null; analysis?: AIUsage | null } | null;
  /** Null for analyses made before requests were recorded. */
  request: AnalysisRequestRecord | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
}

/** A model an analysis may use; prices in USD per million tokens. */
export interface AnalysisModel {
  id: string;
  label: string;
  note: string;
  input_usd: number;
  output_usd: number;
}

export interface LotAnalyses {
  /** The latest completed analysis, shown on the lot page. */
  latest: VehicleAnalysis | null;
  /** The latest request, whatever its status: in progress, or failed after `latest`. */
  current: VehicleAnalysis | null;
}

export type DownloaderStatus =
  | "not_installed" | "paused" | "not_running" | "downloading" | "requested" | "scheduled";

export interface Downloader {
  installed: boolean;
  status: DownloaderStatus;
  /** Server clock when computed: the countdown corrects for a skewed browser clock with it. */
  now?: string;
  heartbeat_at?: string | null;
  daemon_started_at?: string | null;
  next_run_at?: string | null;
  last_success_at?: string | null;
  last_attempt_at?: string | null;
  consecutive_failures?: number;
  needs_attention?: string | null;
  run_requested_at?: string | null;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type Params = Record<string, string | number | undefined | null>;

function withParams(path: string, params?: Params): string {
  if (!params) return path;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") message = data.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export const api = {
  me: () => request<{ username: string }>("GET", "/api/auth/me"),
  login: (username: string, password: string) =>
    request<{ username: string }>("POST", "/api/auth/login", { username, password }),
  logout: () => request<void>("POST", "/api/auth/logout"),
  overview: () => request<Overview>("GET", "/api/overview"),
  downloader: () => request<Downloader>("GET", "/api/downloader"),
  downloadNow: () => request<Downloader>("POST", "/api/downloader/run"),
  runs: (params?: Params) =>
    request<{ items: Run[]; total: number }>("GET", withParams("/api/runs", params)),
  run: (id: number) => request<RunDetail>("GET", `/api/runs/${id}`),
  retryRun: (id: number) => request<{ status: string; file: string }>("POST", `/api/runs/${id}/retry`),
  runRows: (id: number, params: Params) =>
    request<RunRows>("GET", withParams(`/api/runs/${id}/rows`, params)),
  alerts: (params?: Params) =>
    request<{ items: Alert[]; total: number }>("GET", withParams("/api/alerts", params)),
  ackAlert: (id: number) => request<Alert>("POST", `/api/alerts/${id}/ack`),
  resolveAlert: (id: number) => request<Alert>("POST", `/api/alerts/${id}/resolve`),
  schema: (params?: Params) => request<{ items: Column[] }>("GET", withParams("/api/schema", params)),
  lots: (params?: Params) => request<{ items: Lot[]; total: number }>("GET", withParams("/api/lots", params)),
  lotFilters: () => request<LotFilterOptions>("GET", "/api/lot-filters"),
  lot: (source: string, lotNumber: string) =>
    request<LotDetail>("GET", `/api/lots/${encodeURIComponent(source)}/${encodeURIComponent(lotNumber)}`),
  lotImages: (source: string, lotNumber: string) =>
    request<{ images: LotImage[]; complete: boolean }>(
      "GET", `/api/lots/${encodeURIComponent(source)}/${encodeURIComponent(lotNumber)}/images`),
  lotAnalyses: (source: string, lotNumber: string) =>
    request<LotAnalyses>(
      "GET", `/api/lots/${encodeURIComponent(source)}/${encodeURIComponent(lotNumber)}/analyses`),
  analysisModels: () => request<{ default: string; models: AnalysisModel[] }>("GET", "/api/analysis-models"),
  requestAnalysis: (source: string, lotNumber: string, model: string) =>
    request<VehicleAnalysis>(
      "POST", `/api/lots/${encodeURIComponent(source)}/${encodeURIComponent(lotNumber)}/analyses`, { model }),
};

export { withParams };
