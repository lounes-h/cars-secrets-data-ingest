import { ago, bytes, clockTime, compact, dateTime, duration, int, money } from "./format";

describe("format", () => {
  it("compacts only large numbers", () => {
    expect(compact(1284)).toBe("1,284");
    expect(compact(142_335)).toBe("142.3K");
    expect(compact(null)).toBe("—");
  });

  it("formats integers, bytes and durations", () => {
    expect(int(24569)).toBe("24,569");
    expect(bytes(89_601_012)).toBe("85.5 MB");
    expect(bytes(512)).toBe("512 B");
    expect(duration(12.14)).toBe("12.1s");
    expect(duration(125)).toBe("2m 5s");
    expect(duration(null)).toBe("—");
  });

  it("describes elapsed time", () => {
    const now = new Date("2026-09-17T12:00:00Z");
    expect(ago("2026-09-17T11:59:50Z", now)).toBe("just now");
    expect(ago("2026-09-17T11:40:00Z", now)).toBe("20 min ago");
    expect(ago("2026-09-17T09:00:00Z", now)).toBe("3 h ago");
    expect(ago("2026-09-15T12:00:00Z", now)).toBe("2 d ago");
    expect(ago(null, now)).toBe("never");
  });

  it("formats money in the lot currency and tolerates junk", () => {
    expect(money("8119", "USD")).toBe("$8,119");
    expect(money("100", "CAD")).toBe("CA$100");
    expect(money(null, "USD")).toBe("—");
    expect(money("N/A", "USD")).toBe("N/A");
    expect(money("5", null)).toBe("$5");
  });

  it("shows a dash for a missing size or time", () => {
    expect(bytes(null)).toBe("—");
    expect(dateTime(null)).toBe("—");
    expect(clockTime(undefined)).toBe("—");
  });
});
