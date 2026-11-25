"""Per-API-key sliding-window rate limits in Redis (shared by all API processes).

Unlike the LLM limiter (which waits), the API must answer immediately: over
the limit -> 429 with Retry-After.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Final

from redis import Redis

KEY_PREFIX: Final = "api:rate"


class RateClass(StrEnum):
    UPLOAD = "upload"
    WRITE = "write"
    READ = "read"
    SEARCH = "search"  # stricter: a semantic query spends embedding quota


class ApiRateLimiter:
    def __init__(self, redis: Redis, *, window_s: float, clock: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._window_s = window_s
        self._clock = clock

    def hit(self, subject: str, rate_class: RateClass, limit: int) -> int:
        """Record one request. Returns 0 when allowed, else seconds to wait."""
        key = f"{KEY_PREFIX}:{rate_class}:{subject}"
        now = self._clock()
        member = f"{now}:{uuid.uuid4().hex}"
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, now - self._window_s)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.zrange(key, 0, 0, withscores=True)
        pipe.expire(key, math.ceil(self._window_s) + 1)
        _, _, count, oldest, _ = pipe.execute()
        if int(count) <= limit:
            return 0
        self._redis.zrem(key, member)  # a rejected request doesn't consume a slot
        oldest_ts = float(oldest[0][1]) if oldest else now
        return max(1, math.ceil(oldest_ts + self._window_s - now))
