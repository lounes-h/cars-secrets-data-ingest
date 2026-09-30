import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { ToastProvider } from "../components/Toasts";
import { api, type DownloadIssue, type Run, type RunDetail, type RunLot, type RunRows } from "../lib/api";
import { RunDetailPage, RunsPage } from "./Runs";

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

it("shows everything a failed run left behind and puts its file back on retry", async () => {
  const failed = {
    ...detail.run, status: "failed" as const, archived_path: "failed/export.csv", error: "bad header",
    rows_removal_deferred: 4, field_errors: { odometer: 3 }, columns: ["Lot number", "VIN"],
    removal_skipped: Array.from({ length: 13 }, (_, i) => `YARD ${i}`),
  };
  vi.spyOn(api, "run").mockResolvedValue({
    run: failed,
    alerts: [
      { id: 1, severity: "critical", type: "file_failed", title: "Export failed", created_at: "", resolved_at: null },
      { id: 2, severity: "info", type: "yard", title: "Yard skipped", created_at: "", resolved_at: "2026-09-19T12:00:00Z" },
    ],
  });
  vi.spyOn(api, "runRows").mockImplementation((_id, params) => Promise.resolve(pages[String(params.kind)]));
  const retry = vi.spyOn(api, "retryRun")
    .mockRejectedValueOnce(new Error("File is gone"))
    .mockResolvedValue({ status: "queued", file: "export.csv" });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/runs/42"]}>
          <Routes><Route path="/runs/:id" element={<RunDetailPage />} /></Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );

  expect(await screen.findByText("bad header")).toBeInTheDocument();
  expect(screen.getByText("4 lots (auction not over yet)")).toBeInTheDocument();
  expect(screen.getByText(/13 yard\(s\): YARD 0, .*YARD 11…/)).toBeInTheDocument();
  expect(screen.getByText("odometer: 3")).toBeInTheDocument();
  expect(screen.getByText("Export failed")).toBeInTheDocument();
  expect(screen.getByText("resolved")).toBeInTheDocument();
  expect(screen.getByText("2 columns in this file")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Retry this file" }));
  expect(await screen.findByText("File is gone")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retry this file" }));
  expect(await screen.findByText("export.csv is back in the inbox and will be ingested again."))
    .toBeInTheDocument();
  expect(retry).toHaveBeenCalledWith(42);
});

it("cuts long rejected rows short and opens a lot from its row", async () => {
  const raw = Array.from({ length: 13 }, (_, i) => `v${i}`);
  const nameless = { ...lot("555"), year: null, make: null, model_group: null };
  show({
    ...pages,
    new: { ...pages.new, items: [...pages.new.items, nameless] },
    rejected: { items: [{ line_no: 9, reason: "too many fields", raw }], total: 1200, available: true, reason: null },
  }, { ...detail.run, field_errors: null });
  fireEvent.click(await screen.findByRole("button", { name: "Rejected (1,200)" }));
  expect(await screen.findByText(/v11 \| …$/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "New (60)" }));
  fireEvent.click((await screen.findAllByText("2013 ACURA ILX"))[0]);
  // The lot's page is not routed here, so leaving the run page is the navigation.
  await waitFor(() => expect(screen.queryByText("What this run did")).toBeNull());
});

it("shows the load error of a run", async () => {
  vi.spyOn(api, "run").mockRejectedValue(new Error("Run not found"));
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/runs/1"]}>
        <Routes><Route path="/runs/:id" element={<RunDetailPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Run not found")).toBeInTheDocument();
});

it("says so when a run rejected no row or removed no lot", async () => {
  const empty: RunRows = { items: [], total: 0, available: true, reason: null };
  show({ ...pages, rejected: empty, removed: empty });
  fireEvent.click(await screen.findByRole("button", { name: "Rejected (1,200)" }));
  expect(await screen.findByText("No rejected rows.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Removed (2)" }));
  expect(await screen.findByText("No removed lots.")).toBeInTheDocument();
});

describe("runs list", () => {
  function list(items: (Run | DownloadIssue)[], total = items.length) {
    vi.spyOn(api, "downloader").mockResolvedValue({ installed: false, status: "not_installed" });
    const runs = vi.spyOn(api, "runList").mockResolvedValue({ items, total });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/runs"]}>
          <Routes>
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:id" element={<p>run page</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return runs;
  }

  it("lists runs, filters by status and opens a run on click", async () => {
    const runs = list([detail.run]);
    expect(await screen.findByRole("link", { name: "42" })).toHaveAttribute("href", "/runs/42");
    expect(screen.getByText("export.csv")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Failed" }));
    await waitFor(() => expect(runs).toHaveBeenLastCalledWith({ status: "failed", limit: 50, offset: 0 }));
    fireEvent.click(await screen.findByText("export.csv"));
    expect(await screen.findByText("run page")).toBeInTheDocument();
  });

  it("lists downloads that delivered no file between the runs, without a run page", async () => {
    const runs = list([
      { ...detail.run, kind: "file" },
      { kind: "download", id: 42, source: "copart", status: "missed",
        received_at: "2026-09-19T11:00:00Z",
        error: "Not downloaded at the planned time: the Mac was asleep." },
    ]);
    const message = await screen.findByText("Not downloaded at the planned time: the Mac was asleep.");
    expect(screen.getByText("No file:")).toBeInTheDocument();
    expect(screen.getByText("missed")).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(1);  // only the file's run opens a page
    fireEvent.click(message);
    expect(screen.queryByText("run page")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Missed" }));
    await waitFor(() => expect(runs).toHaveBeenLastCalledWith({ status: "missed", limit: 50, offset: 0 }));
  });

  it("says when there are no runs", async () => {
    list([]);
    expect(await screen.findByText("No runs yet.")).toBeInTheDocument();
    expect(await screen.findByText("not set up")).toBeInTheDocument();
  });
});
