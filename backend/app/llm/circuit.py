"""Per-model circuit breaker (Redis-backed, shared by all workers).

After `threshold` consecutive failures a model is skipped for `open_s`
seconds, so a dead free endpoint stops eating quota and latency. The open
deadline is stored as a timestamp from the injected clock (not a Redis TTL),
which keeps the behaviour deterministic under a fake clock. After the
deadline the next call is a trial: success closes the breaker, failure
re-opens it immediately.
"""

from __future__ import annotations

from redis import Redis

from app.llm.clock import Clock

KEY_PREFIX = "llm:circuit"
STATE_TTL_S = 24 * 3600


class CircuitBreaker:
    def __init__(self, redis: Redis, clock: Clock, *, threshold: int, open_s: float) -> None:
        self._redis = redis
        self._clock = clock
        self._threshold = threshold
        self._open_s = open_s

    def is_open(self, model: str) -> bool:
        until = self._redis.get(f"{KEY_PREFIX}:{model}:open_until")
        return until is not None and self._clock.now() < float(until)

    def record_success(self, model: str) -> None:
        self._redis.delete(f"{KEY_PREFIX}:{model}:failures", f"{KEY_PREFIX}:{model}:open_until")

    def record_failure(self, model: str) -> None:
        failures = int(self._redis.incr(f"{KEY_PREFIX}:{model}:failures"))
        self._redis.expire(f"{KEY_PREFIX}:{model}:failures", STATE_TTL_S)
        if failures >= self._threshold:
            self._redis.set(
                f"{KEY_PREFIX}:{model}:open_until", self._clock.now() + self._open_s, ex=STATE_TTL_S
            )
