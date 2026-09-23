import { MutationObserver, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { ApiError } from "./lib/api";

let client: QueryClient | undefined;
vi.mock("./App", () => ({
  App: () => {
    client = useQueryClient();
    return null;
  },
}));

it("mounts the app and signs out on any expired session", async () => {
  document.body.innerHTML = '<div id="root"></div>';
  await import("./main");
  await waitFor(() => expect(client).toBeDefined());
  const qc = client!;
  const expired = () => Promise.reject(new ApiError(401, "expired"));

  qc.setQueryData(["me"], { username: "admin" });
  await qc.fetchQuery({ queryKey: ["x"], queryFn: expired }).catch(() => {});
  expect(qc.getQueryData(["me"])).toBeNull();

  qc.setQueryData(["me"], { username: "admin" });
  await new MutationObserver(qc, { mutationFn: expired }).mutate().catch(() => {});
  expect(qc.getQueryData(["me"])).toBeNull();

  // Other failures leave the session alone.
  qc.setQueryData(["me"], { username: "admin" });
  await qc.fetchQuery({ queryKey: ["y"], queryFn: () => Promise.reject(new Error("boom")) }).catch(() => {});
  expect(qc.getQueryData(["me"])).toEqual({ username: "admin" });
});
