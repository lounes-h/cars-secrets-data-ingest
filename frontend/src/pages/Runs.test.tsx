import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api, type RunDetail } from "../lib/api";
import { RunDetailPage } from "./Runs";

const detail: RunDetail = {
  run: {
    id: 42, source: "copart", filename: "export.csv", sha256: "abc", size_bytes: 1000,
    status: "succeeded", received_at: "2026-09-19T12:00:00Z", started_at: null,
    finished_at: null, duration_seconds: 1, rows_total: 100, rows_new: 0, rows_updated: 0,
    rows_unchanged: 100, rows_rejected: 0, rows_removed: 2, rows_removal_deferred: 0,
    removal_skipped: [], error: null, columns: null, field_errors: {}, archived_path: null,
    removed_lot_numbers: ["41633106", "68264956"],
  },
  rejected_rows: [], alerts: [],
};

function show(run = detail.run) {
  vi.spyOn(api, "run").mockResolvedValue({ ...detail, run });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/runs/42"]}>
        <Routes><Route path="/runs/:id" element={<RunDetailPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

it("shows removed lot numbers as links to their lot details", async () => {
  show();
  for (const lot of detail.run.removed_lot_numbers!) {
    expect(await screen.findByRole("link", { name: lot }))
      .toHaveAttribute("href", `/lots/copart/${lot}`);
  }
});

it("explains when an older run has no recorded lot numbers", async () => {
  show({ ...detail.run, removed_lot_numbers: null });
  expect(await screen.findByText("The complete removed-lot list is unavailable for this older run."))
    .toBeInTheDocument();
});

it("does not show a removed-lots list when no lots were removed", async () => {
  show({ ...detail.run, rows_removed: 0, removed_lot_numbers: [] });
  await screen.findByRole("heading", { name: /Run #42/ });
  expect(screen.queryByRole("heading", { name: /Removed lots/ })).not.toBeInTheDocument();
  expect(screen.queryByText(/complete removed-lot list is unavailable/)).not.toBeInTheDocument();
});
