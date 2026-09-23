import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { api, type Column } from "../lib/api";
import { SchemaPage } from "./Schema";

const mapped: Column = {
  source: "copart", normalized_name: "lot_number", source_name: "Lot number", mapped_to: "lot_number",
  status: "active", first_seen_at: "2026-09-15T10:00:00Z", last_seen_at: "2026-09-19T10:00:00Z",
  first_file_id: 1, last_file_id: 9,
};
const unmapped: Column = {
  ...mapped, normalized_name: "grid_row", source_name: "Grid/Row", mapped_to: null,
  status: "missing", first_file_id: null, last_file_id: null,
};

function show(items: Column[]) {
  vi.spyOn(api, "schema").mockResolvedValue({ items });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><SchemaPage /></MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

it("lists every column a provider sent, what it feeds and whether it is still sent", async () => {
  show([mapped, unmapped]);
  expect(await screen.findByText(/2 columns · 1 unmapped · 1 missing from the latest file/))
    .toBeInTheDocument();
  expect(screen.getByText("attributes only")).toBeInTheDocument();
  expect(screen.getByText("present")).toBeInTheDocument();
  expect(screen.getByText("missing")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "#1" })).toHaveAttribute("href", "/runs/1");
  expect(screen.getByRole("link", { name: "#9" })).toHaveAttribute("href", "/runs/9");
});

it("says when nothing has been ingested", async () => {
  show([]);
  expect(await screen.findByText("No file ingested yet.")).toBeInTheDocument();
});
