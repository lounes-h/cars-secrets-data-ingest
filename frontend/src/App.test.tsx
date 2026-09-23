import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";
import { App } from "./App";
import { api, ApiError, type Overview } from "./lib/api";

const overview: Overview = {
  health: "healthy", worker: null, providers: [], now: "2026-09-19T12:00:00Z",
  lots: { active: 10, removed: 2, new_24h: 1, removed_24h: 0, changed_24h: 3 },
  downloader: { installed: false, status: "not_installed" },
  alerts_open: { info: 0, warning: 1, critical: 2 },
};

/** Stands in for the browser's EventSource; the test drives it through `last`. */
class FakeEventSource {
  static last: FakeEventSource;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((msg: { data: string }) => void) | null = null;
  close = vi.fn();
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
}

function show(path = "/") {
  window.history.pushState({}, "", path);
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.spyOn(api, "overview").mockResolvedValue(overview);
  vi.spyOn(api, "runs").mockResolvedValue({ items: [], total: 0 });
  vi.spyOn(api, "alerts").mockResolvedValue({ items: [], total: 0 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("asks for a sign-in, shows the error, then opens the dashboard", async () => {
  const me = vi.spyOn(api, "me").mockRejectedValue(new ApiError(401, "not authenticated"));
  const login = vi.spyOn(api, "login").mockRejectedValueOnce(new Error("Invalid credentials"));
  show();
  expect(screen.getByText("Loading…")).toBeInTheDocument();
  fireEvent.change(await screen.findByLabelText("Password"), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Invalid credentials");

  login.mockResolvedValue({ username: "admin" });
  me.mockResolvedValue({ username: "admin" });
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: "admin" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  expect(login).toHaveBeenLastCalledWith("admin", "pw");
});

it("sends an unknown path home and counts open alerts in the menu", async () => {
  vi.spyOn(api, "me").mockResolvedValue({ username: "admin" });
  show("/nowhere");
  expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  expect(window.location.pathname).toBe("/");
  expect(await screen.findByLabelText("3 open")).toBeInTheDocument();
});

it("shows the live connection and pops up new alerts, but not info or repeats", async () => {
  vi.spyOn(api, "me").mockResolvedValue({ username: "admin" });
  show();
  await screen.findByRole("heading", { name: "Overview" });
  expect(FakeEventSource.last.url).toBe("/api/events");
  expect(screen.getByText("Reconnecting…")).toBeInTheDocument();

  const send = (data: string) => act(() => FakeEventSource.last.onmessage?.({ data }));
  act(() => FakeEventSource.last.onopen?.());
  expect(screen.getByText("Live")).toBeInTheDocument();

  send("not json");
  send(JSON.stringify({ type: "alert", new: true, severity: "info", title: "Quiet" }));
  send(JSON.stringify({ type: "alert", new: false, severity: "critical", title: "Repeat" }));
  send(JSON.stringify({ type: "alert", new: true, severity: "critical", title: "Worker stopped" }));
  expect(screen.getByText("Worker stopped")).toBeInTheDocument();
  expect(screen.queryByText("Quiet")).toBeNull();
  expect(screen.queryByText("Repeat")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByText("Worker stopped")).toBeNull();

  act(() => FakeEventSource.last.onerror?.());
  expect(screen.getByText("Reconnecting…")).toBeInTheDocument();
});

it("signs out back to the login screen", async () => {
  const me = vi.spyOn(api, "me").mockResolvedValue({ username: "admin" });
  const logout = vi.spyOn(api, "logout").mockResolvedValue(undefined);
  show();
  await screen.findByRole("heading", { name: "Overview" });
  me.mockRejectedValue(new ApiError(401, "not authenticated"));
  fireEvent.click(screen.getByRole("link", { name: "Sign out" }));
  expect(await screen.findByLabelText("Password")).toBeInTheDocument();
  expect(logout).toHaveBeenCalled();
  await waitFor(() => expect(FakeEventSource.last.close).toHaveBeenCalled());
});
