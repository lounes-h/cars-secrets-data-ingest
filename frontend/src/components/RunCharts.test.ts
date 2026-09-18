import type { Run } from "../lib/api";
import { isInitialLoad, toPoints } from "./RunCharts";

const run = (over: Partial<Run>): Run => ({
  id: 1, source: "copart", filename: "f.csv", sha256: "", size_bytes: null, status: "succeeded",
  received_at: "2026-09-17T10:00:00Z", started_at: null, finished_at: null, duration_seconds: 3,
  rows_total: 100, rows_new: 0, rows_updated: 0, rows_unchanged: 0, rows_rejected: 0,
  rows_removed: 0, rows_removal_deferred: 0, removal_skipped: [], error: null, ...over,
});

describe("run chart data", () => {
  it("recognizes a provider's first load", () => {
    expect(isInitialLoad(run({ rows_new: 142_329 }))).toBe(true);
    expect(isInitialLoad(run({ rows_new: 50, rows_updated: 300, rows_unchanged: 24_000 }))).toBe(false);
    expect(isInitialLoad(run({ rows_new: 0, rows_unchanged: 100 }))).toBe(false);
  });

  it("plots succeeded runs oldest first", () => {
    const points = toPoints([
      run({ id: 3, rows_updated: 5 }), run({ id: 2, status: "failed" }), run({ id: 1, rows_new: 7 }),
    ]);
    expect(points.map((p) => p.label)).toEqual(["#1", "#3"]);
  });
});
