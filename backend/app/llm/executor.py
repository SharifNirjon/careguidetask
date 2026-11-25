"""Retry/budget policy for ONE model, shared by the chat and embedding gateways.

Per attempt: reserve daily quota (raise before sending if spent), wait for a
rate-limit slot, call the provider. Failures are handled by kind:

  429           wait Retry-After / X-RateLimit-Reset (if reasonable), retry
  429 daily cap mark the day spent, raise QuotaExhaustedError
  5xx/timeout   exponential backoff + jitter, up to LLM_MAX_ATTEMPTS
  400           BadRequest (the caller may switch output mode)
  401/402/403   LLMAuthError (permanent)

When retries run out the model is "unavailable" and the gateway moves on.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import TypeVar

from app.config import LLMSettings
from app.llm.budget import DailyQuota, SlidingWindowRateLimiter, is_free_model, next_utc_midnight
from app.llm.clock import Clock, utc_datetime
from app.llm.errors import LLMAuthError, QuotaExhaustedError
from app.llm.types import ProviderError, ProviderErrorKind

log = logging.getLogger(__name__)
R = TypeVar("R")

DAILY_CAP_MARKERS = ("per-day", "per day", "daily")
TRANSIENT_KINDS = frozenset(
    {ProviderErrorKind.SERVER, ProviderErrorKind.TIMEOUT, ProviderErrorKind.CONNECTION}
)


class BadRequest(Exception):
    """Internal: the provider rejected the request shape (400)."""


class ModelUnavailable(Exception):
    """Internal: this model is not answering right now; try the next one."""


def backoff_delay(attempt: int, *, base_s: float, max_s: float, rng: random.Random) -> float:
    """Full-jitter exponential backoff: spreads retries from many workers."""
    return rng.uniform(0, min(max_s, base_s * 2 ** (attempt - 1)))


def is_daily_cap(error: ProviderError, now: float, window_s: float) -> bool:
    """A 429 for the daily free-model cap, not the per-minute one."""
    if any(marker in error.message.lower() for marker in DAILY_CAP_MARKERS):
        return True
    return error.reset_at_s is not None and error.reset_at_s - now > 2 * window_s


def rate_limit_wait(error: ProviderError, now: float, *, default_s: float) -> float:
    if error.retry_after_s is not None:
        return error.retry_after_s
    if error.reset_at_s is not None:
        return max(error.reset_at_s - now, 0.0)
    return default_s


class RequestExecutor:
    def __init__(
        self,
        *,
        limiter: SlidingWindowRateLimiter,
        quota: DailyQuota,
        clock: Clock,
        settings: LLMSettings,
        rng: random.Random | None = None,
    ) -> None:
        self._limiter = limiter
        self._quota = quota
        self._clock = clock
        self._settings = settings
        self._rng = rng or random.Random()

    def run(self, model: str, call: Callable[[], R], *, purpose: str) -> tuple[R, int]:
        """Returns (result, attempts). Raises LLMAuthError, QuotaExhaustedError,
        BadRequest or ModelUnavailable."""
        attempt = 0
        while True:
            attempt += 1
            if is_free_model(model):
                self._quota.reserve()
                self._limiter.acquire()
            started = self._clock.now()
            try:
                result = call()
            except ProviderError as error:
                self._log(model, purpose, attempt, started, error.kind.value)
                self._handle(model, error, attempt)
                continue
            self._log(model, purpose, attempt, started, "ok")
            return result, attempt

    def _handle(self, model: str, error: ProviderError, attempt: int) -> None:
        """Sleep before a retry, or raise to stop retrying this model."""
        settings = self._settings
        now = self._clock.now()
        if error.kind is ProviderErrorKind.AUTH:
            raise LLMAuthError(f"provider refused the API key (HTTP {error.status})")
        if error.kind is ProviderErrorKind.BAD_REQUEST:
            raise BadRequest(error.message)
        if error.kind is ProviderErrorKind.RATE_LIMITED and is_daily_cap(error, now, settings.rate_window_s):
            self._quota.mark_exhausted()
            raise QuotaExhaustedError(next_utc_midnight(utc_datetime(self._clock)))
        if attempt >= settings.llm_max_attempts:
            raise ModelUnavailable(f"{model}: {error.kind} after {attempt} attempts")
        if error.kind is ProviderErrorKind.RATE_LIMITED:
            wait = rate_limit_wait(error, now, default_s=settings.llm_backoff_base_s)
            if wait > settings.llm_max_retry_after_s:
                raise ModelUnavailable(f"{model}: rate limited for {wait:.0f}s")
            self._clock.sleep(wait)
        elif error.kind in TRANSIENT_KINDS:
            self._clock.sleep(
                backoff_delay(
                    attempt,
                    base_s=settings.llm_backoff_base_s,
                    max_s=settings.llm_backoff_max_s,
                    rng=self._rng,
                )
            )
        else:
            raise ModelUnavailable(f"{model}: {error.kind}")

    def _log(self, model: str, purpose: str, attempt: int, started: float, outcome: str) -> None:
        log.info(
            "llm request",
            extra={
                "model": model,
                "purpose": purpose,
                "attempt": attempt,
                "outcome": outcome,
                "latency_ms": int((self._clock.now() - started) * 1000),
            },
        )
