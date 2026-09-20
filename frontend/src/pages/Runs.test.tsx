import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { api, type RunDetail, type RunLot, type RunRows } from "../lib/api";
import { RunDetailPage } from "./Runs";

const detail: RunDetail = {
  run: {
    id: 42, source: "copart", filename: "export.csv", sha256: "abc", size_bytes: 1000,
    status: "succeeded", received_at: "2026-09-19T12:00:00Z", started_at: null,
    finished_at: null, duration_seconds: 1, rows_total: 100, rows_new: 60, rows_updated: 3,
    rows_unchanged: 35, rows_rejected: 1200, rows_removed: 2, rows_removal_deferred: 0,
    removal_skipped: [], error: null, columns: null, field_errors: {}, archived_path: null,
  },
  alerts: [],
};

const REASON = "Missing from this export, and its sale time plus the 120-minute grace period "
  + "had passed, so bidding was over.";

function lot(lot_number: string): RunLot {
  return {
    source: "copart", lot_number, year: 2013, make: "ACURA", model_group: "ILX", trim: null,
    vin: "JH4CU2F65DC010051", sale_status: "Pure Sale", sale_at: "2026-09-18T15:00:00Z",
    yard_name: "CA - VALLEJO", removed_at: "2026-09-19T11:00:00Z",
  };
}

const pages: Record<string, RunRows> = {
  new: { items: [lot("111"), lot("222")], total: 60, available: true, reason: null },
  updated: { items: [lot("333")], total: 3, available: true, reason: null },
  removed: {
    items: [lot("41633106"), lot("68264956")], total: 2, available: true, reason: REASON,
  },
  rejected: {
    items: [{ line_no: 7, reason: "expected 59 fields, got 58", raw: ["4163", "ACURA"] }],
    total: 1000, available: true, reason: null,
  },
};

function show(rows: Record<string, RunRows> = pages, run = detail.run) {
  vi.spyOn(api, "run").mockResolvedValue({ ...detail, run });
  const runRows = vi.spyOn(api, "runRows")
    .mockImplementation((_id, params) => Promise.resolve(rows[String(params.kind)]));
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/runs/42"]}>
        <Routes><Route path="/runs/:id" element={<RunDetailPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return runRows;
}

afterEach(() => vi.restoreAllMocks());

it("lists the new lots of a run first, with every list's count on its tab", async () => {
  show();
  expect(await screen.findByRole("link", { name: "111" })).toHaveAttribute("href", "/lots/copart/111");
  expect(screen.getByRole("link", { name: "222" })).toBeInTheDocument();
  for (const name of ["New (60)", "Updated (3)", "Removed (2)", "Rejected (1,200)"]) {
    expect(screen.getByRole("button", { name })).toBeInTheDocument();
  }
  expect(api.runRows).toHaveBeenCalledWith(42, { kind: "new", limit: 50, offset: 0 });
});

it("pages within a list and starts the next list at its first page", async () => {
  show();
  fireEvent.click(await screen.findByRole("button", { name: "Next" }));
  await waitFor(() => expect(api.runRows)
    .toHaveBeenLastCalledWith(42, { kind: "new", limit: 50, offset: 50 }));

  fireEvent.click(screen.getByRole("button", { name: "Updated (3)" }));
  await waitFor(() => expect(api.runRows)
    .toHaveBeenLastCalledWith(42, { kind: "updated", limit: 50, offset: 0 }));
  expect(await screen.findByRole("link", { name: "333" })).toBeInTheDocument();
  // Three updated lots fit on one page, so there is nothing to page through.
  expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
});

it("says why the removed lots were removed and links each one", async () => {
  show();
  fireEvent.click(await screen.findByRole("button", { name: "Removed (2)" }));
  expect(await screen.findByText(`Reason for removal: ${REASON}`)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "41633106" }))
    .toHaveAttribute("href", "/lots/copart/41633106");
});

it("explains when an older run's removed lots cannot be rebuilt", async () => {
  show({ ...pages, removed: { items: [], total: 2, available: false, reason: REASON } });
  fireEvent.click(await screen.findByRole("button", { name: "Removed (2)" }));
  expect(await screen.findByText("The complete removed-lot list is unavailable for this older run."))
    .toBeInTheDocument();
});

it("shows each rejected row's reason and how many rejections were not kept", async () => {
  show();
  fireEvent.click(await screen.findByRole("button", { name: "Rejected (1,200)" }));
  expect(await screen.findByText("expected 59 fields, got 58")).toBeInTheDocument();
  expect(screen.getByText(/200 more were rejected but not stored/)).toBeInTheDocument();
});

it("shows an empty list rather than a pager when a run changed nothing", async () => {
  const empty: RunRows = { items: [], total: 0, available: true, reason: null };
  show({ new: empty, updated: empty, removed: empty, rejected: empty },
    { ...detail.run, rows_new: 0, rows_updated: 0, rows_removed: 0, rows_rejected: 0 });
  expect(await screen.findByText("No new lots.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
});
