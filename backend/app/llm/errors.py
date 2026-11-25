"""Gateway errors. Callers branch on the type, never on messages or HTTP codes."""

from __future__ import annotations

from datetime import datetime


class LLMError(Exception):
    """Base class for every gateway failure."""


class LLMAuthError(LLMError):
    """401/402/403: bad key, no credits or forbidden. Retrying cannot help."""


class QuotaExhaustedError(LLMError):
    """The free-tier daily budget is spent. Raised BEFORE sending a request."""

    def __init__(self, resets_at: datetime) -> None:
        super().__init__(f"free-model daily quota exhausted; resets at {resets_at.isoformat()}")
        self.resets_at = resets_at


class LLMUnavailableError(LLMError):
    """Every configured model failed or is circuit-broken. Transient: retry later."""

    def __init__(self, failures: dict[str, str]) -> None:
        super().__init__(f"no model available: {failures}")
        self.failures = failures


class ExtractionParseError(LLMError):
    """The model's output did not validate, even after one repair attempt."""

    def __init__(self, errors: str, raw_text: str) -> None:
        super().__init__(f"model output failed validation: {errors}")
        self.errors = errors
        self.raw_text = raw_text


class EmbeddingDimensionError(LLMError):
    """A model's native dimension is below EMBEDDING_DIM: fail fast, never pad."""
