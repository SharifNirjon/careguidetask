import createClient, { type Middleware } from "openapi-fetch";

import { networkError, toAppError } from "./errors";
import type { components, paths } from "./schema";

/**
 * Typed API client. Every call goes to this app's own /api/proxy (the browser
 * never talks to the backend and never sees the API key). Generated types come
 * from docs/openapi.json via `npm run gen:api`: no hand-written API types.
 */
export type Schemas = components["schemas"];

export const PROXY_PREFIX = "/api/proxy";

function baseUrl(): string {
  // openapi-fetch builds absolute Request URLs; in the browser this is our own origin.
  const origin = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  return `${origin}${PROXY_PREFIX}`;
}

/** Throw AppError for every non-2xx (304 is success: "unchanged"). */
const throwOnError: Middleware = {
  async onResponse({ response }) {
    if (response.ok || response.status === 304) {
      return undefined;
    }
    const body: unknown = await response
      .clone()
      .json()
      .catch(() => null);
    throw toAppError(response.status, body, response.headers.get("x-request-id"));
  },
  onError({ error }) {
    throw networkError(error);
  },
};

let client: ReturnType<typeof createClient<paths>> | undefined;

export function api(): ReturnType<typeof createClient<paths>> {
  if (!client) {
    client = createClient<paths>({ baseUrl: baseUrl(), credentials: "same-origin" });
    client.use(throwOnError);
  }
  return client;
}

/** For tests: forget the client so the next call picks up a new origin / fetch. */
export function resetApiClient(): void {
  client = undefined;
}

/** URL of a binary endpoint (images, downloads) through the proxy. */
export function proxyUrl(path: string): string {
  return `${PROXY_PREFIX}${path}`;
}

export function newRequestId(): string {
  return crypto.randomUUID().replaceAll("-", "");
}
