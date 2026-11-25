/**
 * Conditional GETs. The API answers 304 (no body) when If-None-Match matches,
 * which keeps polling cheap; this remembers the last ETag + body per URL so a
 * 304 resolves to the previous data.
 */
interface Entry<T> {
  etag: string;
  data: T;
}

const store = new Map<string, Entry<unknown>>();

export interface ConditionalResult<T> {
  data: T;
  changed: boolean;
}

export async function conditionalGet<T>(
  key: string,
  fetcher: (headers: Record<string, string>) => Promise<{ data?: T; response: Response }>,
): Promise<ConditionalResult<T>> {
  const previous = store.get(key) as Entry<T> | undefined;
  const { data, response } = await fetcher(previous ? { "If-None-Match": previous.etag } : {});
  if (response.status === 304 && previous) {
    return { data: previous.data, changed: false };
  }
  if (data === undefined) {
    throw new Error(`Empty response for ${key}`);
  }
  const etag = response.headers.get("etag");
  if (etag) {
    store.set(key, { etag, data });
  }
  return { data, changed: true };
}

export function forgetEtag(prefix: string): void {
  for (const key of store.keys()) {
    if (key.startsWith(prefix)) {
      store.delete(key);
    }
  }
}

/** Polling backoff: 1 s, then x1.5 per unchanged poll, capped at 5 s. */
const FIRST_DELAY_MS = 1000;
const MAX_DELAY_MS = 5000;
const GROWTH = 1.5;

export function pollDelay(unchangedPolls: number): number {
  return Math.min(MAX_DELAY_MS, Math.round(FIRST_DELAY_MS * GROWTH ** Math.max(0, unchangedPolls)));
}
