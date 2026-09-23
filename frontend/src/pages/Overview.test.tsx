import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { api, type Alert, type Overview, type Run } from "../lib/api";
import { downloaderLine, OverviewPage } from "./Overview";

const base: Overview = {
  health: "healthy",
  worker: {
    worker_id: "w1", state: "idle", started_at: "2026-09-19T10:00:00Z",
    last_beat_at: "2026-09-19T11:59:00Z", healthy: true,
  },
  providers: [
    {
      source: "copart", display_name: "Copart", lots_active: 24_000, stale_after_minutes: 90,
      last_success_at: "2026-09-19T11:00:00Z",
      last_run: {
        id: 7, filename: "copart.csv", status: "succeeded", received_at: "2026-09-19T11:00:00Z",
        finished_at: null, rows_total: 24_500, rows_new: 12, rows_updated: 30, rows_removed: 1,
        rows_rejected: 0,
      },
    },
    {
      source: "iaai", display_name: "IAAI", lots_active: 0, stale_after_minutes: 90,
      last_success_at: null, last_run: null,
    },
  ],
  lots: { active: 24_000, removed: 150_000, new_24h: 12, removed_24h: 1, changed_24h: 30 },
  downloader: { installed: true, status: "downloading" },
  alerts_open: { info: 0, warning: 0, critical: 0 },
  now: "2026-09-19T12:00:00Z",
};

const run: Run = {
  id: 7, source: "copart", filename: "copart.csv", sha256: "", size_bytes: null,
  status: "succeeded", received_at: "2026-09-19T11:00:00Z", started_at: null, finished_at: null,
  duration_seconds: 3, rows_total: 24_500, rows_new: 12, rows_updated: 30, rows_unchanged: 24_000,
  rows_rejected: 0, rows_removed: 1, rows_removal_deferred: 0, removal_skipped: [], error: null,
};

const alert = {
  id: 1, severity: "warning", title: "Row count dropped", last_seen_at: "2026-09-19T11:30:00Z",
} as Alert;

function show(data: Overview, alerts: Alert[] = []) {
  vi.spyOn(api, "overview").mockResolvedValue(data);
  vi.spyOn(api, "runs").mockResolvedValue({ items: [run], total: 1 });
  vi.spyOn(api, "alerts").mockResolvedValue({ items: alerts, total: alerts.length });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><OverviewPage /></MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

it("shows a healthy pipeline, each provider's last file and the charts", async () => {
  show(base);
  expect(await screen.findByText("Pipeline healthy")).toBeInTheDocument();
  expect(screen.getByText(/Worker running · last heartbeat .+ ago · idle/)).toBeInTheDocument();
  expect(screen.getByText("Downloader running · downloading now")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "copart.csv" })).toHaveAttribute("href", "/runs/7");
  expect(screen.getByText("none yet")).toBeInTheDocument();
  expect(screen.getByText("150K removed overall")).toBeInTheDocument();
  expect(await screen.findByText("Lot changes per run")).toBeInTheDocument();
  expect(screen.getByText("Nothing open.")).toBeInTheDocument();
});

it("links critical and warning counts to the alerts and lists the open ones", async () => {
  show({ ...base, health: "down", worker: null, alerts_open: { info: 0, warning: 2, critical: 1 } }, [alert]);
  expect(await screen.findByText("Pipeline needs attention")).toBeInTheDocument();
  expect(screen.getByText("No worker has reported yet.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "1 critical, 2 warning →" })).toHaveAttribute("href", "/alerts");
  expect(await screen.findByText("Row count dropped")).toBeInTheDocument();
});

it("names a degraded pipeline and a stopped worker", async () => {
  show({
    ...base, health: "degraded", worker: { ...base.worker!, healthy: false },
    alerts_open: { info: 0, warning: 3, critical: 0 },
  });
  expect(await screen.findByText("Pipeline degraded")).toBeInTheDocument();
  expect(screen.getByText(/Worker not running/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "3 warning →" })).toBeInTheDocument();
});

it("shows the load error instead of the page", async () => {
  vi.spyOn(api, "overview").mockRejectedValue(new Error("Service unavailable"));
  vi.spyOn(api, "runs").mockResolvedValue({ items: [], total: 0 });
  vi.spyOn(api, "alerts").mockResolvedValue({ items: [], total: 0 });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><OverviewPage /></MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
});

describe("downloader line", () => {
  const text = (d: Overview["downloader"]) => {
    const { container, unmount } = render(<p>{downloaderLine(d)[1]}</p>);
    const out = container.textContent;
    unmount();
    return out;
  };

  it("words every downloader state", () => {
    expect(text({ installed: true, status: "scheduled", next_run_at: null }))
      .toBe("Downloader running · next download at —");
    expect(text({ installed: true, status: "requested" })).toBe("Downloader running · download requested");
    expect(text({ installed: true, status: "not_running", heartbeat_at: null }))
      .toMatch(/^Downloader not running · it restarts/);
    expect(text({ installed: true, status: "not_running", heartbeat_at: "2026-09-19T11:00:00Z" }))
      .toMatch(/^Downloader not running since .+ · it restarts/);
    expect(text({ installed: false, status: "not_installed" })).toMatch(/not set up/);
  });
});
