import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Downloader } from "../lib/api";
import { countdown } from "../lib/format";
import { downloaderLine } from "../pages/Overview";
import { NextDownload, viewAt } from "./NextDownload";

const base: Downloader = {
  installed: true, status: "scheduled", now: "2026-09-18T03:00:00Z",
  next_run_at: "2026-09-18T03:30:00Z", heartbeat_at: "2026-09-18T02:59:50Z",
};
const at = (iso: string) => Date.parse(iso);

describe("next download view", () => {
  it("counts down to the planned download, then says it is starting", () => {
    expect(viewAt(base, at("2026-09-18T03:10:00Z"))).toMatchObject({ kind: "countdown", remainingMs: 20 * 60_000 });
    expect(viewAt(base, at("2026-09-18T03:30:01Z")).kind).toBe("due");
  });

  it("maps every server status", () => {
    expect(viewAt({ ...base, status: "downloading" }, 0).kind).toBe("downloading");
    expect(viewAt({ ...base, status: "requested" }, 0).kind).toBe("requested");
    expect(viewAt({ ...base, status: "paused", needs_attention: "challenge" }, 0)).toEqual({ kind: "paused", reason: "challenge" });
    expect(viewAt({ ...base, status: "not_running" }, 0)).toEqual({ kind: "not_running", since: base.heartbeat_at });
    expect(viewAt({ installed: false, status: "not_installed" }, 0).kind).toBe("not_installed");
  });
});

describe("overview downloader line", () => {
  it("uses a status tone per state", () => {
    expect(downloaderLine(base)[0]).toBe("good");
    expect(downloaderLine({ ...base, status: "not_running" })[0]).toBe("critical");
    expect(downloaderLine({ ...base, status: "paused" })[0]).toBe("critical");
    expect(downloaderLine({ installed: false, status: "not_installed" })[0]).toBe("neutral");
  });
});

describe("Download now", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts a request and shows it as requested", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      const body = init?.method === "POST" ? { ...base, status: "requested" } : base;
      return new Response(JSON.stringify(body));
    }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><NextDownload /></QueryClientProvider>);
    fireEvent.click(await screen.findByText("Download now"));
    await waitFor(() => expect(screen.getByText("requested")).toBeInTheDocument());
    expect(calls).toContain("POST /api/downloader/run");
    expect(screen.queryByText("Download now")).toBeNull(); // no double request
  });

  it("is not offered while paused", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...base, status: "paused" }))));
    const qc = new QueryClient();
    render(<QueryClientProvider client={qc}><NextDownload /></QueryClientProvider>);
    expect(await screen.findByText("paused")).toBeInTheDocument();
    expect(screen.queryByText("Download now")).toBeNull();
  });
});

describe("countdown", () => {
  it("formats minutes and hours, never negative", () => {
    expect(countdown(65_000)).toBe("1:05");
    expect(countdown(3_729_000)).toBe("1:02:09");
    expect(countdown(-5_000)).toBe("0:00");
  });
});
