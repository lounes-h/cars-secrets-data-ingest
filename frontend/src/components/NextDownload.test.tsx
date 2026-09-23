import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Downloader } from "../lib/api";
import { countdown } from "../lib/format";
import { downloaderLine } from "../pages/Overview";
import { NextDownload, pauseReason, viewAt } from "./NextDownload";
import { ToastProvider } from "./Toasts";

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

describe("pause reason", () => {
  it("names each reason downloads stop for", () => {
    expect(pauseReason("challenge")).toMatch(/verification page/);
    expect(pauseReason("sign_in_rejected")).toMatch(/rejected the saved login/);
    expect(pauseReason("login_required")).toMatch(/saved login did not work/);
    expect(pauseReason(null)).toMatch(/log in/);
  });

  it("is shown on the overview line", () => {
    const [tone, text] = downloaderLine({ ...base, status: "paused", needs_attention: "sign_in_rejected" });
    expect(tone).toBe("critical");
    render(<p>{text}</p>);
    expect(screen.getByText(/rejected the saved login/)).toBeInTheDocument();
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

describe("next download card", () => {
  afterEach(() => vi.unstubAllGlobals());

  function card(d: Downloader, post?: () => Response) {
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === "POST" && post ? post() : new Response(JSON.stringify(d))));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ToastProvider><NextDownload /></ToastProvider>
      </QueryClientProvider>,
    );
  }

  it("counts down, with the failed attempts it is retrying after", async () => {
    const soon = new Date(Date.now() + 10 * 60_000).toISOString();
    card({ ...base, now: undefined, next_run_at: soon, consecutive_failures: 1, last_success_at: base.heartbeat_at });
    expect(await screen.findByRole("timer")).toBeInTheDocument();
    expect(screen.getByText(/Retrying after 1 failed attempt\./)).toBeInTheDocument();
    expect(screen.getByText(/Last successful download/)).toBeInTheDocument();
  });

  it("pluralises several failed attempts", async () => {
    card({ ...base, now: undefined, next_run_at: new Date(Date.now() + 60_000).toISOString(), consecutive_failures: 3 });
    expect(await screen.findByText(/Retrying after 3 failed attempts\./)).toBeInTheDocument();
  });

  it("says it is starting when the planned time has passed, and toasts a refused request", async () => {
    card({ ...base, next_run_at: null }, () => new Response(JSON.stringify({ detail: "Downloader is busy" }), { status: 409 }));
    expect(await screen.findByText("starting now")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Download now"));
    expect(await screen.findByText("Downloader is busy")).toBeInTheDocument();
  });

  it("describes a download in progress", async () => {
    card({ ...base, status: "downloading" });
    expect(await screen.findByText("downloading…")).toBeInTheDocument();
  });

  it("says since when the downloader has been silent, or that it never reported", async () => {
    card({ ...base, status: "not_running" });
    expect(await screen.findByText("not running")).toBeInTheDocument();
    expect(screen.getByText(/No heartbeat since/)).toBeInTheDocument();
    cleanup();
    card({ ...base, status: "not_running", heartbeat_at: null });
    expect(await screen.findByText(/No heartbeat yet/)).toBeInTheDocument();
  });

  it("explains how to install a missing downloader", async () => {
    card({ installed: false, status: "not_installed" });
    expect(await screen.findByText("not set up")).toBeInTheDocument();
    expect(screen.queryByText("Download now")).toBeNull();
  });
});
