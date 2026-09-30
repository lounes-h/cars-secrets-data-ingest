import type { ReactNode } from "react";
import type { DownloadStatus, RunStatus, Severity } from "../lib/api";

type Tone = "critical" | "warning" | "info" | "good" | "neutral";

// Status is never color alone: every badge carries an icon and a word.
export function Icon({ tone }: { tone: Tone }) {
  const common = { className: "icon", viewBox: "0 0 16 16", "aria-hidden": true } as const;
  switch (tone) {
    case "critical":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" />
          <path d="M5.5 5.5l5 5m0-5l-5 5" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      );
    case "warning":
      return (
        <svg {...common}>
          <path d="M8 1.5l7 12.5H1z" fill="currentColor" />
          <path d="M8 6v3.5" stroke="#000" strokeWidth="1.6" strokeLinecap="round" />
          <circle cx="8" cy="11.8" r="0.9" fill="#000" />
        </svg>
      );
    case "good":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" />
          <path d="M4.8 8.2l2.2 2.2 4.2-4.6" stroke="#fff" strokeWidth="1.8" fill="none"
            strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "info":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" />
          <path d="M8 7.2v4" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" />
          <circle cx="8" cy="4.8" r="1" fill="#fff" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" />
        </svg>
      );
  }
}

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  const cls = tone === "neutral" ? "badge muted" : `badge sev-${tone}`;
  return (
    <span className={cls}>
      <Icon tone={tone} />
      <span style={{ color: "var(--ink)" }}>{children}</span>
    </span>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  const label = { critical: "Critical", warning: "Warning", info: "Info" }[severity];
  return <Badge tone={severity}>{label}</Badge>;
}

const RUN_TONE: Record<RunStatus | DownloadStatus, Tone> = {
  succeeded: "good",
  failed: "critical",
  duplicate: "info",
  interrupted: "warning",
  processing: "neutral",
  missed: "warning",
  skipped: "neutral",
  paused: "critical",
};

export function RunStatusBadge({ status }: { status: RunStatus | DownloadStatus }) {
  return <Badge tone={RUN_TONE[status]}>{status}</Badge>;
}

export function StatTile({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  );
}

export function Pager({ offset, limit, total, onChange }: {
  offset: number; limit: number; total: number; onChange: (offset: number) => void;
}) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(total / limit);
  return (
    <div className="pager">
      <button className="small" disabled={offset === 0} onClick={() => onChange(offset - limit)}>
        Previous
      </button>
      <span className="small">Page {page} of {pages}</span>
      <button className="small" disabled={page >= pages} onClick={() => onChange(offset + limit)}>
        Next
      </button>
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} aria-pressed={value === o.value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function QueryState({ isLoading, error }: { isLoading: boolean; error: unknown }) {
  if (isLoading) return <div className="empty">Loading…</div>;
  if (error) return <div className="empty error">{(error as Error).message}</div>;
  return null;
}
