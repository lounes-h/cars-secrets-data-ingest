import { fireEvent, render, screen } from "@testing-library/react";
import type { Run } from "../lib/api";
import { ChangesTooltip, DurationTooltip, isInitialLoad, RunCharts, toPoints } from "./RunCharts";

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

describe("RunCharts", () => {
  it("says so when no run succeeded yet", () => {
    render(<RunCharts runs={[run({ status: "failed" })]} />);
    expect(screen.getByText(/No successful runs yet/)).toBeInTheDocument();
  });

  it("leaves initial loads out of the chart, and switches to a table and back", () => {
    render(<RunCharts runs={[run({ id: 2, rows_updated: 5 }), run({ id: 1, rows_new: 142_329 })]} />);
    expect(screen.getByText("Not shown: initial load #1 (142.3K new lots).")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show table" }));
    expect(screen.getByRole("heading", { name: "Recent runs" })).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show charts" }));
    expect(screen.getByText("Lot changes per run")).toBeInTheDocument();
  });

  it("names every initial load when there are several", () => {
    render(<RunCharts runs={[run({ id: 2, rows_new: 10 }), run({ id: 1, rows_new: 20 })]} />);
    expect(screen.getByText("Not shown: initial loads #2 (10 new lots), #1 (20 new lots).")).toBeInTheDocument();
  });
});

describe("chart tooltips", () => {
  const [point] = toPoints([run({ id: 5, rows_new: 3, rows_updated: 1_200, duration_seconds: 75 })]);

  it("show the hovered run's changes and duration", () => {
    render(<>
      <ChangesTooltip active payload={[{ payload: point }]} />
      <DurationTooltip active payload={[{ payload: point }]} />
    </>);
    expect(screen.getAllByText(/Run #5/)).toHaveLength(2);
    expect(screen.getByText("1,200")).toBeInTheDocument();
    expect(screen.getByText("1m 15s")).toBeInTheDocument();
  });

  it("render nothing when not hovered", () => {
    const { container } = render(<>
      <ChangesTooltip active={false} payload={[{ payload: point }]} />
      <DurationTooltip active payload={[]} />
    </>);
    expect(container).toBeEmptyDOMElement();
  });
});
