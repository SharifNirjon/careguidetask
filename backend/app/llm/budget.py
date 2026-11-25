"""Free-tier request budget shared by ALL workers (Redis).

OpenRouter counts every request to a `:free` model, failed ones included,
against one account-wide budget: ~20/minute and 50 or 1,000/day. Two guards:

- SlidingWindowRateLimiter: blocks (via the injected clock) until a slot in
  the per-minute window frees up.
- DailyQuota: refuses to send once the UTC-day counter reaches the limit,
  raising QuotaExhaustedError WITHOUT spending a request. The counter is
  periodically resynced from OpenRouter's /key endpoint, the source of truth.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from redis import Redis
from redis.exceptions import WatchError

from app.llm.clock import Clock, utc_datetime
from app.llm.errors import QuotaExhaustedError

KEY_PREFIX = "llm:budget"
DAY_KEY_TTL_S = 2 * 24 * 3600


def is_free_model(model: str) -> bool:
    return model.endswith(":free")


class SlidingWindowRateLimiter:
    """At most `limit` acquisitions per `window_s`, across processes."""

    def __init__(self, redis: Redis, clock: Clock, *, limit: int, window_s: float) -> None:
        self._redis = redis
        self._clock = clock
        self._limit = limit
        self._window_s = window_s
        self._key = f"{KEY_PREFIX}:window"

    def acquire(self) -> float:
        """Take a slot, sleeping as long as needed. Returns the total seconds waited."""
        waited = 0.0
        while (wait := self._try_acquire()) > 0:
            self._clock.sleep(wait)
            waited += wait
        return waited

    def _try_acquire(self) -> float:
        """0 if a slot was taken, else seconds until the oldest entry leaves the window."""
        with self._redis.pipeline() as pipe:
            while True:
                try:
                    now = self._clock.now()
                    pipe.watch(self._key)  # type: ignore[no-untyped-call]  # redis-py lacks annotations
                    pipe.zremrangebyscore(self._key, 0, now - self._window_s)
                    if pipe.zcard(self._key) < self._limit:
                        pipe.multi()
                        pipe.zadd(self._key, {f"{now}:{uuid.uuid4().hex}": now})
                        pipe.expire(self._key, int(self._window_s) + 1)
                        pipe.execute()
                        return 0.0
                    oldest = pipe.zrange(self._key, 0, 0, withscores=True)
                    pipe.unwatch()
                    oldest_ts = float(oldest[0][1]) if oldest else now
                    return max(oldest_ts + self._window_s - now, 0.001)
                except WatchError:
                    continue  # another worker raced us; re-read


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


class QuotaSource(Protocol):
    """Authoritative daily counter (OpenRouter /key). None when unavailable."""

    def fetch(self) -> QuotaStatus | None: ...


def next_utc_midnight(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)


class DailyQuota:
    """UTC-day request counter with a hard stop before the limit."""

    def __init__(
        self,
        redis: Redis,
        clock: Clock,
        *,
        default_limit: int,
        source: QuotaSource | None = None,
        resync_s: float,
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._default_limit = default_limit
        self._source = source
        self._resync_s = resync_s

    def _day(self) -> str:
        return utc_datetime(self._clock).date().isoformat()

    def _keys(self) -> tuple[str, str, str]:
        day = self._day()
        return (
            f"{KEY_PREFIX}:day:{day}:used",
            f"{KEY_PREFIX}:day:{day}:limit",
            f"{KEY_PREFIX}:day:{day}:synced_at",
        )

    def status(self) -> QuotaStatus:
        self._maybe_resync()
        used_key, limit_key, _ = self._keys()
        used, limit = self._redis.mget(used_key, limit_key)
        return QuotaStatus(int(used or 0), int(limit or self._default_limit))

    def reserve(self) -> None:
        """Count one request about to be sent, or raise if the budget is spent."""
        self._maybe_resync()
        used_key, limit_key, _ = self._keys()
        limit = int(self._redis.get(limit_key) or self._default_limit)
        used = int(self._redis.incr(used_key))
        self._redis.expire(used_key, DAY_KEY_TTL_S)
        if used > limit:
            self._redis.decr(used_key)
            raise QuotaExhaustedError(next_utc_midnight(utc_datetime(self._clock)))

    def mark_exhausted(self) -> None:
        """The provider says the day is spent (429 on the daily cap): stop sending."""
        used_key, limit_key, _ = self._keys()
        limit = int(self._redis.get(limit_key) or self._default_limit)
        self._redis.set(used_key, limit, ex=DAY_KEY_TTL_S)

    def resync(self) -> QuotaStatus | None:
        """Adopt the provider's counter (never lowering what we already counted)."""
        if self._source is None:
            return None
        remote = self._source.fetch()
        used_key, limit_key, synced_key = self._keys()
        self._redis.set(synced_key, self._clock.now(), ex=DAY_KEY_TTL_S)
        if remote is None:
            return None
        local_used = int(self._redis.get(used_key) or 0)
        self._redis.set(used_key, max(local_used, remote.used), ex=DAY_KEY_TTL_S)
        self._redis.set(limit_key, remote.limit, ex=DAY_KEY_TTL_S)
        return QuotaStatus(max(local_used, remote.used), remote.limit)

    def _maybe_resync(self) -> None:
        if self._source is None:
            return
        _, _, synced_key = self._keys()
        synced_at = self._redis.get(synced_key)
        if synced_at is None or self._clock.now() - float(synced_at) >= self._resync_s:
            self.resync()
