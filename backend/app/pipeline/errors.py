"""Pipeline error taxonomy.

Two families, and only one is retried:

- PermanentError: the input itself is bad (corrupt, encrypted, too big, ...).
  Retrying cannot help, so the document fails immediately.
- TransientError: infrastructure hiccups (storage, DB, a single slow OCR page).
  Celery retries these with exponential backoff + jitter.

Every error carries a machine `code`, a user-safe `message` (no paths, no stack
traces, no library internals) and internal `details` for logs and events.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.enums import ErrorCode

USER_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.UNSUPPORTED_TYPE: "This file type is not supported.",
    ErrorCode.TYPE_MISMATCH: "The file content does not match its extension.",
    ErrorCode.EMPTY_FILE: "The file is empty.",
    ErrorCode.FILE_TOO_LARGE: "The file exceeds the maximum allowed size.",
    ErrorCode.TOO_MANY_PAGES: "The document has more pages than allowed.",
    ErrorCode.CORRUPT_FILE: "The file is damaged and could not be read.",
    ErrorCode.PASSWORD_PROTECTED: "The file is password protected.",
    ErrorCode.NO_TEXT_FOUND: "No readable text was found in the file.",
    ErrorCode.DECOMPRESSION_BOMB: "The file expands to an unsafe size and was rejected.",
    ErrorCode.STORAGE_ERROR: "The file could not be stored or read. Please try again.",
    ErrorCode.TIMEOUT: "Processing took too long.",
    ErrorCode.WORKER_TIMEOUT: "Processing stopped responding and was cancelled. It can be reprocessed.",
    ErrorCode.OCR_ERROR: "Text recognition failed for part of the file.",
    ErrorCode.INTERNAL_ERROR: "An unexpected error occurred while processing the file.",
    ErrorCode.LLM_ERROR: "Automatic data extraction failed for this file.",
    ErrorCode.LLM_UNAVAILABLE: "The extraction service is temporarily unavailable. It will retry.",
    ErrorCode.LLM_QUOTA_DEFERRED: (
        "Daily extraction capacity is used up; the file will be processed after it resets."
    ),
}


class PipelineError(Exception):
    """Base class. Do not raise directly; use PermanentError or TransientError."""

    retryable: bool = False

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message or USER_MESSAGES.get(code, USER_MESSAGES[ErrorCode.INTERNAL_ERROR])
        self.details = details or {}
        super().__init__(f"{code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


class PermanentError(PipelineError):
    """The input is unusable. Fail the document; never retry."""

    retryable = False


class TransientError(PipelineError):
    """Temporary infrastructure failure. Safe to retry."""

    retryable = True


class OcrTimeoutError(TransientError):
    """A single page exceeded OCR_PAGE_TIMEOUT_S (retried once at page level)."""

    def __init__(self, timeout_s: int) -> None:
        super().__init__(ErrorCode.TIMEOUT, details={"timeout_s": timeout_s})


class OcrEngineError(PipelineError):
    """The OCR engine failed on one page. Recorded on the unit; the job continues."""

    def __init__(self, reason: str) -> None:
        super().__init__(ErrorCode.OCR_ERROR, details={"reason": reason})


class DeferredError(PipelineError):
    """Not a failure: the work must wait until `resume_at` (e.g. the LLM daily quota resets)."""

    def __init__(self, resume_at: datetime) -> None:
        super().__init__(ErrorCode.LLM_QUOTA_DEFERRED, details={"resume_at": resume_at.isoformat()})
        self.resume_at = resume_at
