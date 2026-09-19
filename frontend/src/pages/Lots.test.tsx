import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { api } from "../lib/api";
import { LotDetailPage } from "./Lots";

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
