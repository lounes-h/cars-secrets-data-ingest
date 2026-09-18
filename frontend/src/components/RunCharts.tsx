import { useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { Run } from "../lib/api";
import { compact, dateTime, duration, int } from "../lib/format";

// Categorical slots 1-3 of the validated reference palette, in fixed order.
const SERIES = [
  { key: "rows_new", label: "New", color: "var(--series-1)" },
  { key: "rows_updated", label: "Updated", color: "var(--series-2)" },
  { key: "rows_removed", label: "Removed", color: "var(--series-3)" },
] as const;

const axisTick = { fill: "var(--muted)", fontSize: 12 };

interface Point {
  id: number;
  label: string;
  when: string;
  rows_new: number;
  rows_updated: number;
  rows_removed: number;
  rows_total: number;
  duration: number;
}

function toPoints(runs: Run[]): Point[] {
  return runs
    .filter((r) => r.status === "succeeded")
    .slice()
    .reverse()
    .map((r) => ({
      id: r.id,
      label: `#${r.id}`,
      when: dateTime(r.received_at),
      rows_new: r.rows_new ?? 0,
      rows_updated: r.rows_updated ?? 0,
      rows_removed: r.rows_removed ?? 0,
      rows_total: r.rows_total ?? 0,
      duration: r.duration_seconds ?? 0,
    }));
}

function ChangesTooltip({ active, payload }: { active?: boolean; payload?: { payload: Point }[] }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="chart-tooltip">
      <div className="muted">Run {p.label} · {p.when}</div>
      {SERIES.map((s) => (
        <div className="row" key={s.key}>
          <span><span className="key" style={{ background: s.color }} /> {s.label}</span>
          <strong>{int(p[s.key])}</strong>
        </div>
      ))}
      <div className="row muted"><span>Rows in file</span><span>{int(p.rows_total)}</span></div>
    </div>
  );
}

function DurationTooltip({ active, payload }: { active?: boolean; payload?: { payload: Point }[] }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="chart-tooltip">
      <div className="muted">Run {p.label} · {p.when}</div>
      <strong>{duration(p.duration)}</strong>
    </div>
  );
}

function DataTable({ points }: { points: Point[] }) {
  return (
    <div className="table-wrap" style={{ maxHeight: 260, overflowY: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>Run</th><th>Received</th><th className="num">New</th><th className="num">Updated</th>
            <th className="num">Removed</th><th className="num">Duration</th>
          </tr>
        </thead>
        <tbody>
          {points.slice().reverse().map((p) => (
            <tr key={p.id}>
              <td>{p.label}</td><td>{p.when}</td><td className="num">{int(p.rows_new)}</td>
              <td className="num">{int(p.rows_updated)}</td><td className="num">{int(p.rows_removed)}</td>
              <td className="num">{duration(p.duration)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RunCharts({ runs }: { runs: Run[] }) {
  const [asTable, setAsTable] = useState(false);
  const points = toPoints(runs);
  if (points.length === 0) {
    return <div className="card empty">No successful runs yet. Drop an export into the inbox.</div>;
  }
  const toggle = (
    <button className="small chart-toggle" onClick={() => setAsTable((v) => !v)}>
      {asTable ? "Show charts" : "Show table"}
    </button>
  );
  if (asTable) {
    return (
      <div className="card">
        {toggle}
        <h2>Recent runs</h2>
        <DataTable points={points} />
      </div>
    );
  }
  return (
    <div className="grid two-col">
      <div className="card">
        {toggle}
        <h2>Lot changes per run</h2>
        <div className="chart-legend" aria-hidden="true">
          {SERIES.map((s) => (
            <span key={s.key}><i className="swatch" style={{ background: s.color }} />{s.label}</span>
          ))}
        </div>
        <div style={{ height: 240 }} role="img"
          aria-label="Stacked columns of new, updated and removed lots for each recent run">
          <ResponsiveContainer>
            <BarChart data={points} margin={{ top: 4, right: 4, bottom: 0, left: 0 }} barCategoryGap="20%">
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="label" tick={axisTick} tickLine={false} axisLine={{ stroke: "var(--axis)" }}
                minTickGap={12} />
              <YAxis tick={axisTick} tickLine={false} axisLine={false} width={52}
                tickFormatter={(v: number) => compact(v)} />
              <Tooltip content={<ChangesTooltip />} cursor={{ fill: "var(--surface-2)" }} />
              {SERIES.map((s, i) => (
                <Bar key={s.key} dataKey={s.key} stackId="changes" fill={s.color} maxBarSize={24}
                  stroke="var(--surface)" strokeWidth={2} isAnimationActive={false}
                  radius={i === SERIES.length - 1 ? [4, 4, 0, 0] : 0} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="card">
        <h2>Ingest duration</h2>
        <div className="chart-legend muted">Seconds from file pickup to commit</div>
        <div style={{ height: 240 }} role="img" aria-label="Line of ingest duration per recent run">
          <ResponsiveContainer>
            <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="label" tick={axisTick} tickLine={false} axisLine={{ stroke: "var(--axis)" }}
                minTickGap={12} />
              <YAxis tick={axisTick} tickLine={false} axisLine={false} width={44}
                tickFormatter={(v: number) => `${Math.round(v)}s`} />
              <Tooltip content={<DurationTooltip />}
                cursor={{ stroke: "var(--axis)", strokeWidth: 1 }} />
              <Line dataKey="duration" stroke="var(--series-1)" strokeWidth={2} dot={false}
                activeDot={{ r: 5, stroke: "var(--surface)", strokeWidth: 2 }}
                strokeLinecap="round" strokeLinejoin="round" isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
