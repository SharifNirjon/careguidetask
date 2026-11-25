import {
  FORWARDED_REQUEST_HEADERS,
  FORWARDED_RESPONSE_HEADERS,
  isAllowed,
  isMutation,
  isSameOriginRequest,
  pickHeaders,
} from "./proxy-rules";

/**
 * Backend-for-frontend proxy: browser -> /api/proxy/<path> -> BACKEND_URL/<path>.
 * The API key is added here and never leaves the server. Bodies are streamed in
 * both directions (uploads up to the backend's limit never sit in memory).
 * Dependencies are injected so tests run without Next or a backend.
 */
export interface ProxyDeps {
  backendUrl: string;
  apiKey: string;
  expectedOrigin: string;
  isAuthenticated: () => Promise<boolean>;
  fetchImpl?: typeof fetch;
}

const PROXIED_METHODS = new Set(["GET", "POST", "PATCH"]);

function problem(status: number, code: string, detail: string, requestId: string): Response {
  return Response.json(
    {
      type: `about:blank#${code}`,
      title: code,
      status,
      detail,
      instance: "",
      code,
      request_id: requestId,
      errors: [],
    },
    { status, headers: { "content-type": "application/problem+json", "x-request-id": requestId } },
  );
}

export async function handleProxy(
  request: Request,
  path: readonly string[],
  deps: ProxyDeps,
): Promise<Response> {
  const requestId = request.headers.get("x-request-id") ?? crypto.randomUUID().replaceAll("-", "");
  const method = request.method.toUpperCase();
  const target = path.map(encodeURIComponent).join("/");

  if (!(await deps.isAuthenticated())) {
    return problem(401, "unauthenticated", "Your session has expired. Please sign in again.", requestId);
  }
  if (!PROXIED_METHODS.has(method)) {
    return problem(405, "method_not_allowed", "This method is not available.", requestId);
  }
  if (!isAllowed(method, target)) {
    return problem(404, "not_found", "This endpoint is not available.", requestId);
  }
  if (isMutation(method) && !isSameOriginRequest(request.headers, deps.expectedOrigin)) {
    return problem(403, "cross_origin", "Cross-origin requests are not allowed.", requestId);
  }

  const headers = pickHeaders(request.headers, FORWARDED_REQUEST_HEADERS);
  headers.set("authorization", `Bearer ${deps.apiKey}`);
  headers.set("x-request-id", requestId);
  const search = new URL(request.url).search;
  const init: RequestInit & { duplex?: "half" } = {
    method,
    headers,
    redirect: "manual",
    cache: "no-store",
  };
  if (isMutation(method) && request.body !== null) {
    init.body = request.body;
    init.duplex = "half"; // required to stream a request body with fetch
  }

  let upstream: Response;
  try {
    upstream = await (deps.fetchImpl ?? fetch)(
      `${deps.backendUrl.replace(/\/$/, "")}/${target}${search}`,
      init,
    );
  } catch {
    return problem(502, "backend_unavailable", "The service is unreachable. Please try again.", requestId);
  }
  const responseHeaders = pickHeaders(upstream.headers, FORWARDED_RESPONSE_HEADERS);
  const empty = upstream.status === 204 || upstream.status === 304;
  return new Response(empty ? null : upstream.body, { status: upstream.status, headers: responseHeaders });
}
