import { queriesFor } from "./events";

describe("queriesFor", () => {
  it("refreshes run data when a run finishes", () => {
    const keys = queriesFor({ type: "run_finished" }).map((k) => k[0]);
    expect(keys).toEqual(
      expect.arrayContaining(["overview", "runs", "run", "run-rows", "lots", "schema"]));
  });

  it("refreshes alerts and the overview on alert events", () => {
    for (const type of ["alert", "alert_resolved", "alert_updated"] as const) {
      const keys = queriesFor({ type }).map((k) => k[0]);
      expect(keys).toEqual(expect.arrayContaining(["overview", "alerts"]));
    }
  });

  it("refreshes the lot totals, lists and details when sale prices are imported", () => {
    expect(queriesFor({ type: "lots_imported" })).toEqual([["overview"], ["lots"], ["lot"]]);
  });

  it("refreshes a lot's AI analysis on analysis events", () => {
    expect(queriesFor({ type: "analysis" })).toEqual([["lot-analysis"]]);
  });

  it("refreshes the runs list when a download delivered no file", () => {
    expect(queriesFor({ type: "download_issue" })).toEqual([["runs"]]);
  });

  it("refreshes the downloader card on downloader events, and the overview on anything else", () => {
    expect(queriesFor({ type: "downloader" })).toEqual([["downloader"], ["overview"]]);
    expect(queriesFor({ type: "unknown" as never })).toEqual([["overview"]]);
  });
});
