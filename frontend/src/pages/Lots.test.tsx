import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { api, type Overview } from "../lib/api";
import { LotDetailPage, LotsPage } from "./Lots";

afterEach(() => vi.restoreAllMocks());

describe("lot totals", () => {
  const overview: Overview = {
    health: "healthy", worker: null, providers: [], now: "2026-09-19T12:00:00Z",
    lots: { active: 1234, removed: 56, new_24h: 0, removed_24h: 0, changed_24h: 0 },
    downloader: { installed: false, status: "not_installed" },
    alerts_open: { info: 0, warning: 0, critical: 0 },
  };

  function show() {
    vi.spyOn(api, "lots").mockResolvedValue({ items: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><LotsPage /></MemoryRouter>
      </QueryClientProvider>,
    );
    return client;
  }

  it("shows inventory totals independent of results and refreshes them with the overview", async () => {
    const totals = vi.spyOn(api, "overview").mockResolvedValue(overview);
    const client = show();
    expect(await screen.findByRole("button", { name: "All (1,290)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Active (1,234)" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Removed (56)" }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "FORD" } });
    await waitFor(() => expect(api.lots).toHaveBeenLastCalledWith({
      q: "FORD", state: "removed", limit: 50, offset: 0,
    }));
    expect(screen.getByRole("button", { name: "All (1,290)" })).toBeInTheDocument();

    totals.mockResolvedValue({ ...overview, lots: { ...overview.lots, active: 1233, removed: 57 } });
    await client.invalidateQueries({ queryKey: ["overview"] });
    expect(await screen.findByRole("button", { name: "Removed (57)" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Active (1,233)" })).toBeInTheDocument();
  });

  it("shows zero totals for an empty inventory", async () => {
    vi.spyOn(api, "overview").mockResolvedValue({
      ...overview, lots: { ...overview.lots, active: 0, removed: 0 },
    });
    show();
    for (const label of ["All", "Active", "Removed"]) {
      expect(await screen.findByRole("button", { name: `${label} (0)` })).toBeInTheDocument();
    }
  });

  it("keeps the filters usable when totals are unavailable", async () => {
    vi.spyOn(api, "overview").mockRejectedValue(new Error("Unavailable"));
    show();
    await screen.findByText("No lots match.");
    for (const label of ["All", "Active", "Removed"]) {
      expect(screen.getByRole("button", { name: `${label} (—)` })).toBeEnabled();
    }
  });
});

describe("lot detail", () => {
  it("links back to the lots list, even while loading", () => {
    vi.spyOn(api, "lot").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "lotImages").mockReturnValue(new Promise(() => {}));
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/lots/copart/68264956"]}>
          <Routes><Route path="/lots/:source/:lotNumber" element={<LotDetailPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("link", { name: /Lots/ })).toHaveAttribute("href", "/lots");
  });
});
