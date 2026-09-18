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
  rejected_rows: { line_no: number; reason: string; raw: string[] }[];
  alerts: Pick<Alert, "id" | "severity" | "type" | "title" | "created_at" | "resolved_at">[];
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
  alerts: (params?: Params) =>
    request<{ items: Alert[]; total: number }>("GET", withParams("/api/alerts", params)),
  ackAlert: (id: number) => request<Alert>("POST", `/api/alerts/${id}/ack`),
  resolveAlert: (id: number) => request<Alert>("POST", `/api/alerts/${id}/resolve`),
  schema: (params?: Params) => request<{ items: Column[] }>("GET", withParams("/api/schema", params)),
  lots: (params?: Params) => request<{ items: Lot[] }>("GET", withParams("/api/lots", params)),
  lot: (source: string, lotNumber: string) =>
    request<LotDetail>("GET", `/api/lots/${encodeURIComponent(source)}/${encodeURIComponent(lotNumber)}`),
  lotImages: (source: string, lotNumber: string) =>
    request<{ images: LotImage[]; complete: boolean }>(
      "GET", `/api/lots/${encodeURIComponent(source)}/${encodeURIComponent(lotNumber)}/images`),
};

export { withParams };
