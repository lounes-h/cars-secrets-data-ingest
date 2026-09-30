import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { api, type Lot, type LotDetail, type Overview } from "../lib/api";
import { activeCount } from "../components/LotFilters";
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
    vi.spyOn(api, "lots").mockResolvedValue({ items: [], total: 0 });
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
    vi.spyOn(api, "lotAnalyses").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "analysisModels").mockReturnValue(new Promise(() => {}));
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
  function list(items: Lot[], total = items.length) {
    vi.spyOn(api, "overview").mockRejectedValue(new Error("Unavailable"));
    const lots = vi.spyOn(api, "lots").mockResolvedValue({ items, total });
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

  it("shows only favorites when asked, and says so, so filters are not mistaken for all lots", async () => {
    const lots = list([]);
    vi.spyOn(api, "lotFilters").mockResolvedValue({ makes: [], damages: [], states: [], yards: [], keys: [], runs: [] });
    fireEvent.click(await screen.findByRole("button", { name: "☆ Favorites" }));
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({
      q: "", state: "all", limit: 50, offset: 0, favorites: "true" }));
    expect(await screen.findByText(/No favorites match/)).toBeInTheDocument();
    const on = screen.getByRole("button", { name: "★ Favorites only" });
    expect(on).toHaveAttribute("aria-pressed", "true");
    expect(on).toHaveClass("toggle");  // styled as switched on
    expect(screen.getByText("0 favorite lots")).toBeInTheDocument();

    fireEvent.click(on);
    expect(screen.getByRole("button", { name: "☆ Favorites" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    fireEvent.change(screen.getByLabelText("Make"), { target: { value: "HONDA" } });
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({
      q: "", state: "all", limit: 50, offset: 0, make: "HONDA" }));  // every lot, not only favorites
    expect(await screen.findByText("0 lots")).toBeInTheDocument();
  });

  it("pages through every match, and back", async () => {
    const lots = list(Array.from({ length: 50 }, (_, i) => ({ ...lot, lot_number: String(i) })), 120);
    expect(await screen.findByText("Page 1 of 3")).toBeInTheDocument();
    expect(screen.getByText("120 lots")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({ q: "", state: "all", limit: 50, offset: 50 }));
    fireEvent.click(await screen.findByRole("button", { name: "Previous" }));
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({ q: "", state: "all", limit: 50, offset: 0 }));
  });
});

describe("lot filters", () => {
  const options = {
    makes: ["ACURA", "HONDA"], damages: ["FRONT END", "REAR END"], states: ["CA", "TX"],
    yards: ["CA - VALLEJO"], keys: ["NO", "YES"], runs: ["Run & Drive", "Stationary"],
  };

  function page() {
    vi.spyOn(api, "overview").mockRejectedValue(new Error("Unavailable"));
    const lots = vi.spyOn(api, "lots").mockResolvedValue({ items: [lot], total: 1 });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter><LotsPage /></MemoryRouter>
      </QueryClientProvider>,
    );
    return lots;
  }

  it("narrows the list with the chosen filters, counts them, and clears them", async () => {
    vi.spyOn(api, "lotFilters").mockResolvedValue(options);
    const lots = page();
    expect(await screen.findByText("1 lot")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    const panel = screen.getByRole("group", { name: "Lot filters" });
    expect(await within(panel).findByRole("option", { name: "FRONT END" })).toBeInTheDocument();
    expect(panel.querySelector("#filter-makes option[value='HONDA']")).not.toBeNull();

    fireEvent.change(within(panel).getByLabelText("Make"), { target: { value: "HONDA" } });
    fireEvent.change(within(panel).getByLabelText("Primary damage"), { target: { value: "FRONT END" } });
    fireEvent.change(within(panel).getByLabelText("Year from"), { target: { value: "2018" } });
    fireEvent.change(within(panel).getByLabelText("Sale to"), { target: { value: "2026-10-01" } });
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({
      q: "", state: "all", limit: 50, offset: 0,
      make: "HONDA", damage: "FRONT END", year_min: "2018", sale_to: "2026-10-01",
    }));
    expect(screen.getByRole("button", { name: "Filters (4)" })).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith({ q: "", state: "all", limit: 50, offset: 0 }));
    expect(screen.getByRole("button", { name: "Filters" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    expect(screen.queryByRole("group", { name: "Lot filters" })).not.toBeInTheDocument();
  });

  it("still filters by typing when the filter values cannot be loaded", async () => {
    vi.spyOn(api, "lotFilters").mockRejectedValue(new Error("timeout"));
    const lots = page();
    fireEvent.click(await screen.findByRole("button", { name: "Filters" }));
    expect(await screen.findByText("Filter values unavailable: timeout")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Max odometer"), { target: { value: "90000" } });
    await waitFor(() => expect(lots).toHaveBeenLastCalledWith(
      expect.objectContaining({ odometer_max: "90000" })));
  });

  it("counts only filters that hold something", () => {
    expect(activeCount({ make: " ", model: "", yard: "CA - VALLEJO" })).toBe(1);
  });
});

describe("lot detail page", () => {
  const detail: LotDetail = {
    lot: {
      ...lot, attributes: { "Lot number": "68264956", Color: "BLACK" }, first_file_id: 1,
      last_file_id: 9, listing_url: "https://www.copart.com/lot/68264956",
      predicted_final_price: "5230.4",
    },
    history: [
      { changed_at: "2026-09-18T10:00:00Z", file_id: 8, changes: [{ field: "high_bid", old: null, new: "1500" }] },
      { changed_at: "2026-09-17T10:00:00Z", file_id: null, changes: [{ field: "sale_status", old: "On Minimum Bid", new: null }] },
    ],
  };

  function open(data: LotDetail, images = { images: [], complete: true }) {
    vi.spyOn(api, "lot").mockResolvedValue(data);
    vi.spyOn(api, "lotImages").mockResolvedValue(images);
    vi.spyOn(api, "lotAnalyses").mockResolvedValue({ latest: null, current: null });
    vi.spyOn(api, "analysisModels").mockResolvedValue({ default: "claude-opus-5-5", models: [] });
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
    expect(screen.getByText("Predicted price").nextSibling).toHaveTextContent("$5,230");
    expect(screen.getByRole("link", { name: "run #8" })).toHaveAttribute("href", "/runs/8");
    expect(screen.getAllByText(/∅/)).toHaveLength(2);
    expect(screen.getByText("All 2 source values")).toBeInTheDocument();
    const add = vi.spyOn(api, "addFavorite").mockResolvedValue(undefined);
    fireEvent.click(screen.getByRole("button", { name: "Add to favorites" }));
    await waitFor(() => expect(add).toHaveBeenCalledWith("copart", "68264956"));
    expect(await screen.findByText("No photos for this lot.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Generate AI Analysis" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "AI Analysis" })).toBeInTheDocument();
  });

  it("marks a removed lot and names another auction as it is", async () => {
    open({
      ...detail,
      lot: { ...detail.lot, source: "iaai", removed_at: "2026-09-19T11:00:00Z", odometer: null, favorite: true,
        vin: null, damage_primary: null, damage_secondary: null, sale_status: null, yard_name: null,
        predicted_final_price: null },
      history: [],
    });
    expect(await screen.findByText(/^removed /)).toBeInTheDocument();
    expect(screen.getByText("Predicted price").nextSibling).toHaveTextContent("—");
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
