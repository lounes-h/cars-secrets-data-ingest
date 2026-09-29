import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { api, type Lot, type LotDetail, type Overview } from "../lib/api";
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

const lot: Lot = {
  source: "copart", lot_number: "68264956", year: 2013, make: "ACURA", model_group: "TSX",
  model_detail: "TSX TECH", trim: "TECH", vin: "JH4CU2F65DC010051", damage_primary: "FRONT END",
  damage_secondary: "REAR END", odometer: "123456", est_retail_value: "9800", repair_cost: null,
  high_bid: "1500", buy_it_now_price: null, currency: "USD", sale_status: "Pure Sale",
  sale_at: "2026-09-20T15:00:00Z", yard_code: "1", yard_name: "CA - VALLEJO",
  location_city: "VALLEJO", location_state: "CA", thumbnail_url: "https://cs.copart.com/a_thb.jpg",
  first_seen_at: "2026-09-15T10:00:00Z", last_seen_at: "2026-09-19T10:00:00Z",
  last_changed_at: "2026-09-18T10:00:00Z", removed_at: null, favorite: false,
};
const bare: Lot = {
  ...lot, lot_number: "41633106", year: null, model_group: null, vin: null, damage_primary: null,
  odometer: null, high_bid: null, yard_name: null, thumbnail_url: null,
  removed_at: "2026-09-19T11:00:00Z",
};

describe("lot list", () => {
  function list(items: Lot[]) {
    vi.spyOn(api, "overview").mockRejectedValue(new Error("Unavailable"));
    const lots = vi.spyOn(api, "lots").mockResolvedValue({ items });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/lots"]}>
          <Routes>
            <Route path="/lots" element={<LotsPage />} />
            <Route path="/lots/:source/:lotNumber" element={<p>detail page</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return lots;
  }

  it("shows each lot with dashes for what the auction left out, and opens a lot on click", async () => {
    list([lot, bare]);
    expect(await screen.findByRole("link", { name: "68264956" })).toHaveAttribute("href", "/lots/copart/68264956");
    expect(screen.getByText("2013 ACURA TSX")).toBeInTheDocument();
    expect(screen.getByText("123,456")).toBeInTheDocument();
    expect(screen.getByText("$1,500")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("removed")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(3);
    fireEvent.click(screen.getByText("2013 ACURA TSX"));
    expect(await screen.findByText("detail page")).toBeInTheDocument();
  });

  it("marks and unmarks favorites without opening the lot", async () => {
    list([lot, { ...bare, favorite: true }]);
    const add = vi.spyOn(api, "addFavorite").mockResolvedValue(undefined);
    const remove = vi.spyOn(api, "removeFavorite").mockRejectedValue(new Error("server down"));
    const unmark = await screen.findByRole("button", { name: "Remove from favorites" });
    expect(unmark).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Add to favorites" }));
    fireEvent.click(unmark);
    await waitFor(() => expect(add).toHaveBeenCalledWith("copart", "68264956"));
    expect(remove).toHaveBeenCalledWith("copart", "41633106");
    await waitFor(() => expect(unmark).toHaveAttribute("title", "server down"));
    expect(screen.queryByText("detail page")).not.toBeInTheDocument();
  });

  it("shows only favorites when asked", async () => {
    const lots = list([]);
    fireEvent.click(await screen.findByRole("button", { name: "★ Favorites" }));
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({
      q: "", state: "all", limit: 50, offset: 0, favorites: "true" }));
    expect(await screen.findByText(/No favorites match/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "★ Favorites" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "★ Favorites" }));
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({ q: "", state: "all", limit: 50, offset: 0 }));
  });

  it("pages forward while a page is full, and back", async () => {
    const lots = list(Array.from({ length: 50 }, (_, i) => ({ ...lot, lot_number: String(i) })));
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({ q: "", state: "all", limit: 50, offset: 50 }));
    fireEvent.click(await screen.findByRole("button", { name: "Previous" }));
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({ q: "", state: "all", limit: 50, offset: 0 }));
  });
});

describe("lot detail page", () => {
  const detail: LotDetail = {
    lot: {
      ...lot, attributes: { "Lot number": "68264956", Color: "BLACK" }, first_file_id: 1,
      last_file_id: 9, listing_url: "https://www.copart.com/lot/68264956",
    },
    history: [
      { changed_at: "2026-09-18T10:00:00Z", file_id: 8, changes: [{ field: "high_bid", old: null, new: "1500" }] },
      { changed_at: "2026-09-17T10:00:00Z", file_id: null, changes: [{ field: "sale_status", old: "On Minimum Bid", new: null }] },
    ],
  };

  function open(data: LotDetail, images = { images: [], complete: true }) {
    vi.spyOn(api, "lot").mockResolvedValue(data);
    vi.spyOn(api, "lotImages").mockResolvedValue(images);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/lots/copart/68264956"]}>
          <Routes><Route path="/lots/:source/:lotNumber" element={<LotDetailPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("shows the lot, its link to the auction, its history and every source value", async () => {
    open(detail);
    expect(await screen.findByRole("heading", { name: /2013 ACURA TSX TSX TECH · lot 68264956/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View on Copart ↗" })).toHaveAttribute("href", detail.lot.listing_url);
    expect(screen.getByText("FRONT END / REAR END")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "run #8" })).toHaveAttribute("href", "/runs/8");
    expect(screen.getAllByText(/∅/)).toHaveLength(2);
    expect(screen.getByText("All 2 source values")).toBeInTheDocument();
    const add = vi.spyOn(api, "addFavorite").mockResolvedValue(undefined);
    fireEvent.click(screen.getByRole("button", { name: "Add to favorites" }));
    await waitFor(() => expect(add).toHaveBeenCalledWith("copart", "68264956"));
    expect(await screen.findByText("No photos for this lot.")).toBeInTheDocument();
  });

  it("marks a removed lot and names another auction as it is", async () => {
    open({
      ...detail,
      lot: { ...detail.lot, source: "iaai", removed_at: "2026-09-19T11:00:00Z", odometer: null, favorite: true,
        vin: null, damage_primary: null, damage_secondary: null, sale_status: null, yard_name: null },
      history: [],
    });
    expect(await screen.findByText(/^removed /)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View on iaai ↗" })).toBeInTheDocument();
    expect(screen.getByText("Unchanged since first seen.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Favorite" })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows the load error under the back link", async () => {
    vi.spyOn(api, "lot").mockRejectedValue(new Error("Lot not found"));
    vi.spyOn(api, "lotImages").mockResolvedValue({ images: [], complete: true });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/lots/copart/1"]}>
          <Routes><Route path="/lots/:source/:lotNumber" element={<LotDetailPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Lot not found")).toBeInTheDocument();
  });
});
