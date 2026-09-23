import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { api, type Alert } from "../lib/api";
import { AlertsPage } from "./Alerts";

const open: Alert = {
  id: 1, severity: "critical", type: "file_failed", title: "Export failed to load",
  details: { error: "bad header" }, source: "copart", fingerprint: "f1", occurrences: 3,
  file_id: 42, created_at: "2026-09-19T10:00:00Z", last_seen_at: "2026-09-19T11:00:00Z",
  acked_at: null, acked_by: null, resolved_at: null, resolved_by: null,
};
const acked: Alert = {
  ...open, id: 2, severity: "warning", title: "Row count dropped", details: {}, source: null,
  occurrences: 1, file_id: null, acked_at: "2026-09-19T11:00:00Z", acked_by: "admin",
};
const resolved: Alert = {
  ...acked, id: 3, severity: "info", title: "Yard skipped", details: null as never, resolved_at: "2026-09-19T11:30:00Z",
  resolved_by: "system", acked_at: null, acked_by: null,
};

function show(items: Alert[], total = items.length) {
  const alerts = vi.spyOn(api, "alerts").mockResolvedValue({ items, total });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><AlertsPage /></MemoryRouter>
    </QueryClientProvider>,
  );
  return alerts;
}

afterEach(() => vi.restoreAllMocks());

it("lists alerts with their history and details, and acts only on open ones", async () => {
  const ack = vi.spyOn(api, "ackAlert").mockResolvedValue(open);
  const resolve = vi.spyOn(api, "resolveAlert").mockResolvedValue(open);
  const alerts = show([open, acked, resolved]);

  expect(await screen.findByText("Export failed to load")).toBeInTheDocument();
  expect(screen.getByText(/seen 3×/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "run #42" })).toHaveAttribute("href", "/runs/42");
  expect(screen.getByText(/acknowledged by admin/)).toBeInTheDocument();
  expect(screen.getByText(/resolved .* by system/)).toBeInTheDocument();
  expect(screen.getAllByText("Details")).toHaveLength(1);

  // One Acknowledge (the un-acked open alert), two Resolve (both open alerts).
  expect(screen.getAllByRole("button", { name: "Acknowledge" })).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Resolve" })).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
  await waitFor(() => expect(ack).toHaveBeenCalledWith(1));
  fireEvent.click(screen.getAllByRole("button", { name: "Resolve" })[1]);
  await waitFor(() => expect(resolve).toHaveBeenCalledWith(2));
  await waitFor(() => expect(alerts.mock.calls.length).toBeGreaterThan(1));
});

it("filters by state and severity from the first page", async () => {
  const alerts = show([], 120);
  expect(await screen.findByText("No alerts here.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  await waitFor(() => expect(alerts).toHaveBeenLastCalledWith(
    { state: "open", severity: "", limit: 50, offset: 50 }));

  fireEvent.click(screen.getByRole("button", { name: "Resolved" }));
  fireEvent.click(screen.getByRole("button", { name: "Critical" }));
  await waitFor(() => expect(alerts).toHaveBeenLastCalledWith(
    { state: "resolved", severity: "critical", limit: 50, offset: 0 }));
});
