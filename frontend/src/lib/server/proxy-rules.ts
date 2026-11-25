/**
 * The backend-for-frontend proxy's rules (pure, no Next imports: unit-tested).
 *
 * The proxy is NOT open: only these (method, path) pairs reach the backend,
 * only these request headers are forwarded, and the API key is added here, on
 * the server. Paths are matched against the full backend path
 * ("api/v1/documents/<uuid>"), so a crafted path can't escape the API.
 */

const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";

type Method = "GET" | "POST" | "PATCH";

const RULES: ReadonlyArray<readonly [Method, RegExp]> = [
  ["GET", /^api\/v1\/documents$/],
  ["POST", /^api\/v1\/documents$/],
  ["GET", new RegExp(`^api/v1/documents/${UUID}$`)],
  ["GET", new RegExp(`^api/v1/documents/${UUID}/(events|file|results)$`)],
  ["GET", new RegExp(`^api/v1/documents/${UUID}/units/\\d{1,6}/(image|table)$`)],
  ["POST", new RegExp(`^api/v1/documents/${UUID}/reprocess$`)],
  ["GET", new RegExp(`^api/v1/extractions/${UUID}(/versions|/corrections)?$`)],
  ["PATCH", new RegExp(`^api/v1/extractions/${UUID}$`)],
  ["POST", new RegExp(`^api/v1/extractions/${UUID}/(approve|reject)$`)],
  ["POST", new RegExp(`^api/v1/issues/${UUID}/(resolve|reopen)$`)],
  ["GET", new RegExp(`^api/v1/jobs/${UUID}$`)],
  ["POST", /^api\/v1\/reconciliations$/],
  ["GET", new RegExp(`^api/v1/reconciliations/${UUID}$`)],
  ["GET", /^api\/v1\/search$/],
];

export const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "content-type",
  "content-length",
  "if-match",
  "if-none-match",
  "idempotency-key",
  "x-request-id",
] as const;

export const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "content-length",
  "content-disposition",
  "cache-control",
  "etag",
  "retry-after",
  "x-request-id",
] as const;

const SAFE_METHODS = new Set(["GET", "HEAD"]);

export function isAllowed(method: string, path: string): boolean {
  if (path.includes("..") || path.includes("//")) {
    return false;
  }
  return RULES.some(([m, pattern]) => m === method && pattern.test(path));
}

export function isMutation(method: string): boolean {
  return !SAFE_METHODS.has(method);
}

/**
 * CSRF guard for mutations: the browser always sends Origin on cross-origin and
 * on same-origin non-GET fetches. Missing or foreign Origin -> reject. Together
 * with the SameSite=Lax session cookie this blocks cross-site form posts and
 * fetches.
 */
export function isSameOrigin(originHeader: string | null, expectedOrigin: string): boolean {
  return originHeader !== null && originHeader === expectedOrigin;
}

/**
 * CSRF check for state-changing requests. The Origin header must match; when the
 * browser withholds it (`Origin: null`, which Chrome sends for form posts from a
 * `Referrer-Policy: no-referrer` page), Fetch Metadata decides instead: only
 * `Sec-Fetch-Site: same-origin` is accepted. A missing header pair is refused.
 */
export function isSameOriginRequest(headers: Headers, expectedOrigin: string): boolean {
  const origin = headers.get("origin");
  if (origin !== null && origin !== "null") {
    return isSameOrigin(origin, expectedOrigin);
  }
  return headers.get("sec-fetch-site") === "same-origin";
}

export function pickHeaders(source: Headers, names: readonly string[]): Headers {
  const out = new Headers();
  for (const name of names) {
    const value = source.get(name);
    if (value !== null) {
      out.set(name, value);
    }
  }
  return out;
}
