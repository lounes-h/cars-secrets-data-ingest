import { api, ApiError, withParams } from "./api";

describe("withParams", () => {
  it("drops empty values and encodes the rest", () => {
    expect(withParams("/api/runs", { status: "", limit: 50, q: "a b", x: null })).toBe(
      "/api/runs?limit=50&q=a+b",
    );
    expect(withParams("/api/runs")).toBe("/api/runs");
  });
});

describe("request errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("raises ApiError with the server's detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ detail: "not authenticated" }), { status: 401 })));
    await expect(api.overview()).rejects.toMatchObject({ status: 401, message: "not authenticated" });
    await expect(api.overview()).rejects.toBeInstanceOf(ApiError);
  });

  it("sends JSON bodies same-origin", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ username: "admin" })));
    vi.stubGlobal("fetch", fetchMock);
    await api.login("admin", "pw");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/auth/login");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body as string)).toEqual({ username: "admin", password: "pw" });
  });
});

describe("endpoints", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("calls each endpoint with its method and path", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method} ${url}`);
      return new Response(JSON.stringify({}));
    }));
    await api.me();
    await api.downloader();
    await api.downloadNow();
    await api.runs({ limit: 5 });
    await api.run(3);
    await api.retryRun(3);
    await api.runRows(3, { kind: "new" });
    await api.alerts();
    await api.ackAlert(4);
    await api.resolveAlert(4);
    await api.schema();
    await api.lots({ q: "vin" });
    await api.lot("copart", "a/b");
    await api.lotImages("copart", "1");
    await api.lotFilters();
    await api.addFavorite("copart", "1");
    await api.removeFavorite("copart", "1");
    expect(calls).toEqual([
      "GET /api/auth/me", "GET /api/downloader", "POST /api/downloader/run", "GET /api/runs?limit=5",
      "GET /api/runs/3", "POST /api/runs/3/retry", "GET /api/runs/3/rows?kind=new", "GET /api/alerts",
      "POST /api/alerts/4/ack", "POST /api/alerts/4/resolve", "GET /api/schema", "GET /api/lots?q=vin",
      "GET /api/lots/copart/a%2Fb", "GET /api/lots/copart/1/images", "GET /api/lot-filters",
      "PUT /api/lots/copart/1/favorite", "DELETE /api/lots/copart/1/favorite",
    ]);
  });

  it("returns nothing for 204 and keeps the status text for a non-JSON error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    await expect(api.logout()).resolves.toBeUndefined();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>", { status: 502, statusText: "Bad Gateway" })));
    await expect(api.overview()).rejects.toMatchObject({ status: 502, message: "Bad Gateway" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: [{ msg: "x" }] }), { status: 422, statusText: "Unprocessable" })));
    await expect(api.overview()).rejects.toMatchObject({ message: "Unprocessable" });
  });
});
