import { describe, expect, it, vi } from "vitest";

import { handleProxy, type ProxyDeps } from "./proxy-handler";
import { isAllowed, isSameOriginRequest } from "./proxy-rules";

const ORIGIN = "http://localhost:3000";
const DOC = "11111111-1111-4111-8111-111111111111";
const SECRET = "cg_deadbeef_super-secret-api-key-value";

function deps(overrides: Partial<ProxyDeps> = {}) {
  const fetchImpl = vi.fn<typeof fetch>(() =>
    Promise.resolve(
      new Response('{"ok":true}', {
        status: 200,
        headers: {
          "content-type": "application/json",
          etag: '"abc"',
          "set-cookie": "leak=1",
          server: "uvicorn",
        },
      }),
    ),
  );
  return {
    fetchImpl,
    deps: {
      backendUrl: "http://api:8000",
      apiKey: SECRET,
      expectedOrigin: ORIGIN,
      isAuthenticated: () => Promise.resolve(true),
      fetchImpl,
      ...overrides,
    } satisfies ProxyDeps,
  };
}

function call(method: string, path: string[], init: RequestInit = {}, query = "") {
  return new Request(`${ORIGIN}/api/proxy/${path.join("/")}${query}`, { method, ...init });
}

describe("allowlist", () => {
  it.each([
    ["GET", "api/v1/documents", true],
    ["POST", "api/v1/documents", true],
    ["GET", `api/v1/documents/${DOC}/units/3/image`, true],
    ["PATCH", `api/v1/extractions/${DOC}`, true],
    ["DELETE", `api/v1/documents/${DOC}`, false], // admin action: not exposed to the UI
    ["GET", "api/v1/stats", false],
    ["POST", "api/v1/jobs/reprocess-failed", false],
    ["GET", "api/v1/documents/not-a-uuid", false],
    ["GET", `api/v1/documents/${DOC}/../../stats`, false],
    ["GET", "health/ready", false],
  ])("%s %s -> %s", (method, path, allowed) => {
    expect(isAllowed(method, path)).toBe(allowed);
  });
});

describe("handleProxy", () => {
  it("adds the API key server-side and forwards only allowlisted headers", async () => {
    const { deps: d, fetchImpl } = deps();
    const response = await handleProxy(
      call("GET", ["api", "v1", "documents", DOC], {
        headers: {
          "if-none-match": '"x"',
          cookie: "cg_session=s",
          "x-request-id": "req-1",
          authorization: "Bearer spoof",
        },
      }),
      ["api", "v1", "documents", DOC],
      d,
    );
    expect(response.status).toBe(200);
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(url).toBe(`http://api:8000/api/v1/documents/${DOC}`);
    const headers = new Headers(init?.headers);
    expect(headers.get("authorization")).toBe(`Bearer ${SECRET}`); // the browser's value is replaced
    expect(headers.get("if-none-match")).toBe('"x"');
    expect(headers.get("x-request-id")).toBe("req-1");
    expect(headers.get("cookie")).toBeNull(); // the session cookie never reaches the backend
    expect(response.headers.get("etag")).toBe('"abc"');
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(response.headers.get("server")).toBeNull();
  });

  it("forwards the query string", async () => {
    const { deps: d, fetchImpl } = deps();
    await handleProxy(
      call("GET", ["api", "v1", "search"], {}, "?q=acme&mode=keyword"),
      ["api", "v1", "search"],
      d,
    );
    expect(fetchImpl.mock.calls[0]?.[0]).toBe("http://api:8000/api/v1/search?q=acme&mode=keyword");
  });

  it("rejects non-allowlisted paths and methods without calling the backend", async () => {
    const { deps: d, fetchImpl } = deps();
    const path = await handleProxy(call("GET", ["api", "v1", "stats"]), ["api", "v1", "stats"], d);
    const method = await handleProxy(
      call("DELETE", ["api", "v1", "documents", DOC], { headers: { origin: ORIGIN } }),
      ["api", "v1", "documents", DOC],
      d,
    );
    expect(path.status).toBe(404);
    expect(method.status).toBe(405);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects cross-origin mutations (CSRF) and mutations without an Origin", async () => {
    const { deps: d, fetchImpl } = deps();
    const target = ["api", "v1", "extractions", DOC];
    const evil = await handleProxy(
      call("PATCH", target, { headers: { origin: "https://evil.example" }, body: "{}" }),
      target,
      d,
    );
    const none = await handleProxy(call("PATCH", target, { body: "{}" }), target, d);
    const same = await handleProxy(
      call("PATCH", target, { headers: { origin: ORIGIN }, body: "{}" }),
      target,
      d,
    );
    expect([evil.status, none.status, same.status]).toEqual([403, 403, 200]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("requires a session", async () => {
    const { deps: d, fetchImpl } = deps({ isAuthenticated: () => Promise.resolve(false) });
    const response = await handleProxy(
      call("GET", ["api", "v1", "documents"]),
      ["api", "v1", "documents"],
      d,
    );
    expect(response.status).toBe(401);
    expect(await response.json()).toMatchObject({ code: "unauthenticated" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("returns a problem with a request id when the backend is down", async () => {
    const { deps: d } = deps({ fetchImpl: () => Promise.reject(new TypeError("ECONNREFUSED")) });
    const response = await handleProxy(
      call("GET", ["api", "v1", "documents"]),
      ["api", "v1", "documents"],
      d,
    );
    expect(response.status).toBe(502);
    const body = (await response.json()) as { code: string; request_id: string };
    expect(body.code).toBe("backend_unavailable");
    expect(body.request_id).toMatch(/^[0-9a-f]{32}$/);
    expect(JSON.stringify(body)).not.toContain(SECRET);
  });

  it("streams request bodies upstream", async () => {
    const { deps: d, fetchImpl } = deps();
    await handleProxy(
      call("POST", ["api", "v1", "documents"], {
        headers: { origin: ORIGIN, "idempotency-key": "k-12345678" },
        body: "multipart-bytes",
      }),
      ["api", "v1", "documents"],
      d,
    );
    const init = fetchImpl.mock.calls[0]?.[1] as RequestInit & { duplex?: string };
    expect(init.body).toBeInstanceOf(ReadableStream);
    expect(init.duplex).toBe("half");
    expect(new Headers(init.headers).get("idempotency-key")).toBe("k-12345678");
  });
});

describe("isSameOriginRequest (CSRF)", () => {
  const app = "http://localhost:3000";
  const check = (headers: Record<string, string>) => isSameOriginRequest(new Headers(headers), app);

  it("accepts a matching Origin and refuses another", () => {
    expect(check({ origin: app })).toBe(true);
    expect(check({ origin: "https://evil.example" })).toBe(false);
    expect(check({ origin: "https://evil.example", "sec-fetch-site": "same-origin" })).toBe(false);
  });

  it("falls back to Fetch Metadata only when the browser withholds Origin", () => {
    expect(check({ origin: "null", "sec-fetch-site": "same-origin" })).toBe(true);
    expect(check({ "sec-fetch-site": "same-origin" })).toBe(true);
    expect(check({ origin: "null", "sec-fetch-site": "cross-site" })).toBe(false);
    expect(check({ "sec-fetch-site": "same-site" })).toBe(false);
    expect(check({})).toBe(false);
  });
});
