import type { Downloader } from "../lib/api";
import { countdown } from "../lib/format";
import { viewAt } from "./NextDownload";

const base: Downloader = {
  installed: true, status: "scheduled", now: "2026-09-18T03:00:00Z",
  next_run_at: "2026-09-18T03:30:00Z", window_end: "2026-09-18T03:35:00Z", jitter_seconds: 300,
};
const at = (iso: string) => Date.parse(iso);

describe("next download view", () => {
  it("counts down to the next wake-up", () => {
    const v = viewAt(base, at("2026-09-18T03:10:00Z"));
    expect(v).toMatchObject({ kind: "countdown", remainingMs: 20 * 60_000 });
  });

  it("switches to 'starting' at zero, and to 'late' when nothing happened", () => {
    expect(viewAt(base, at("2026-09-18T03:30:00Z")).kind).toBe("starting");
    expect(viewAt(base, at("2026-09-18T03:39:00Z")).kind).toBe("starting");
    expect(viewAt(base, at("2026-09-18T03:41:00Z")).kind).toBe("late");
  });

  it("shows paused and unknown states from the server", () => {
    expect(viewAt({ ...base, status: "paused", needs_attention: "challenge" }, 0))
      .toEqual({ kind: "paused", reason: "challenge" });
    expect(viewAt({ ...base, status: "unknown", installed: false, next_run_at: null }, 0))
      .toEqual({ kind: "unknown", installed: false });
  });
});

describe("countdown", () => {
  it("formats minutes and hours, never negative", () => {
    expect(countdown(65_000)).toBe("1:05");
    expect(countdown(3_729_000)).toBe("1:02:09");
    expect(countdown(400)).toBe("0:01");
    expect(countdown(-5_000)).toBe("0:00");
  });
});
