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
