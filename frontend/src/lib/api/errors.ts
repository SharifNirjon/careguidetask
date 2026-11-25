/**
 * One place that turns API failures (RFC 9457 problem+json) into AppError.
 * Components show `message` and, in details, `requestId`, which support can
 * grep for in the API and worker logs.
 */

export interface FieldError {
  /** e.g. ["body", "fields", "total"] */
  loc: (string | number)[];
  msg: string;
  type: string;
}

/** User-facing messages for stable error codes (problem `code` or pipeline ErrorCode). */
const MESSAGES: Record<string, string> = {
  // pipeline / upload outcomes
  TYPE_MISMATCH: "The file's content doesn't match its extension.",
  UNSUPPORTED_TYPE: "This file type isn't supported. Use PDF, PNG, JPEG, TIFF, XLSX, XLS or CSV.",
  FILE_TOO_LARGE: "The file is larger than the upload limit.",
  EMPTY_FILE: "The file is empty.",
  PASSWORD_PROTECTED: "The file is password protected. Remove the password and upload it again.",
  CORRUPT_FILE: "The file is damaged and couldn't be read.",
  DECOMPRESSION_BOMB: "The file expands to an unsafe size and was rejected.",
  TOO_MANY_PAGES: "The document has more pages than allowed.",
  NO_TEXT_FOUND: "No readable text was found in the file.",
  LLM_QUOTA_DEFERRED:
    "Today's extraction capacity is used up. The file will be processed after the daily reset.",
  LLM_UNAVAILABLE: "The extraction service is temporarily unavailable. It will retry automatically.",
  WORKER_TIMEOUT: "Processing stopped responding. You can reprocess the document.",
  // API problem codes
  unauthenticated: "Your session has expired. Please sign in again.",
  unauthorized: "The service rejected the request's credentials.",
  forbidden: "You don't have permission to do that.",
  rate_limited: "Too many requests. Please wait a moment.",
  version_mismatch: "Someone else changed this invoice. Reload to see their changes.",
  if_match_required: "This change needs the current version. Reload and try again.",
  unresolved_errors: "Resolve or fix the remaining errors before approving.",
  job_active: "This document is already being processed.",
  not_current: "This is an older version; only the current version can be changed.",
  file_too_large: "A file is larger than the upload limit.",
  request_too_large: "The upload is larger than the allowed total size.",
  too_many_files: "Too many files in one upload.",
  idempotency_in_progress: "This upload is still being processed. Please wait.",
  invalid_values: "Some values are not valid. Check the highlighted fields.",
  validation_error: "Some values are not valid.",
  offset_out_of_range: "No more results.",
  backend_unavailable: "The service is unreachable. Please try again.",
  cross_origin: "The request was blocked for security reasons. Reload the page.",
  network_error: "Network error. Check your connection and try again.",
  internal_error: "Something went wrong on our side.",
};

const FALLBACK_MESSAGE = "Something went wrong. Please try again.";

export function messageFor(code: string | null | undefined, fallback?: string): string {
  return (code ? MESSAGES[code] : undefined) ?? fallback ?? FALLBACK_MESSAGE;
}

export class AppError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly title: string,
    readonly detail: string,
    readonly fieldErrors: FieldError[] = [],
    readonly requestId: string | null = null,
  ) {
    super(detail);
    this.name = "AppError";
  }

  /** What to show the user: a known friendly message, else the server's detail. */
  get userMessage(): string {
    return messageFor(this.code, this.detail || undefined);
  }

  /** Field errors keyed by the last path segment ("total", "line_items.0.amount"). */
  fieldMessages(prefix: readonly (string | number)[] = ["body", "fields"]): Record<string, string> {
    const out: Record<string, string> = {};
    for (const e of this.fieldErrors) {
      const startsWith = prefix.every((p, i) => e.loc[i] === p);
      if (startsWith) {
        out[e.loc.slice(prefix.length).join(".")] = e.msg;
      }
    }
    return out;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asFieldErrors(value: unknown): FieldError[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isRecord).map((e) => ({
    loc: Array.isArray(e.loc)
      ? e.loc.filter((p): p is string | number => typeof p === "string" || typeof p === "number")
      : [],
    msg: typeof e.msg === "string" ? e.msg : "Invalid value",
    type: typeof e.type === "string" ? e.type : "value_error",
  }));
}

/** Build an AppError from a status, a (possibly non-problem) body and the response's request id. */
export function toAppError(status: number, body: unknown, requestIdHeader: string | null = null): AppError {
  if (isRecord(body)) {
    const code = typeof body.code === "string" ? body.code : `http_${status}`;
    const requestId = typeof body.request_id === "string" ? body.request_id : requestIdHeader;
    return new AppError(
      status,
      code,
      typeof body.title === "string" ? body.title : "Error",
      typeof body.detail === "string" ? body.detail : messageFor(code),
      asFieldErrors(body.errors),
      requestId,
    );
  }
  return new AppError(status, `http_${status}`, "Error", messageFor(null), [], requestIdHeader);
}

export function networkError(cause: unknown): AppError {
  const error = new AppError(0, "network_error", "Network error", messageFor("network_error"));
  error.cause = cause;
  return error;
}

export function isAppError(error: unknown): error is AppError {
  return error instanceof AppError;
}
