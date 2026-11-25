/**
 * Per-client login attempt limit (in memory, per server process).
 *
 * Enough to slow down password guessing against the single demo account; a
 * multi-instance deployment would move this to Redis like the API's limiter.
 */
const WINDOW_MS = 60_000;
const MAX_ATTEMPTS = 10;

const attempts = new Map<string, number[]>();

export function allowLoginAttempt(client: string, now: number = Date.now()): boolean {
  const recent = (attempts.get(client) ?? []).filter((t) => now - t < WINDOW_MS);
  if (recent.length >= MAX_ATTEMPTS) {
    attempts.set(client, recent);
    return false;
  }
  recent.push(now);
  attempts.set(client, recent);
  return true;
}

export function resetLoginAttempts(client: string): void {
  attempts.delete(client);
}
