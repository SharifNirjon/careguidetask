/**
 * Dates from the API are ISO. Invoice dates (YYYY-MM-DD) are calendar dates
 * with no time zone, so they are formatted in UTC: formatting them in local
 * time would shift "2026-03-01" to Feb 28 west of Greenwich. Timestamps
 * ("...Z") are instants and shown in the user's local time.
 */

const PLACEHOLDER = "—";
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

export function formatInvoiceDate(iso: string | null | undefined, locale?: string): string {
  if (!iso) {
    return PLACEHOLDER;
  }
  const match = ISO_DATE.exec(iso);
  if (!match) {
    return iso;
  }
  const [, y, m, d] = match;
  const date = new Date(Date.UTC(Number(y), Number(m) - 1, Number(d)));
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" }).format(date);
}

export function formatTimestamp(iso: string | null | undefined, locale?: string, timeZone?: string): string {
  if (!iso) {
    return PLACEHOLDER;
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone }).format(date);
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) {
    return PLACEHOLDER;
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  const seconds = ms / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(1)} s`;
  }
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} s`;
}

const BYTE_UNITS = ["B", "KB", "MB", "GB"] as const;
const KIB = 1024;

export function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= KIB && unit < BYTE_UNITS.length - 1) {
    value /= KIB;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${BYTE_UNITS[unit] ?? "B"}`;
}
