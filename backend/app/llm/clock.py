"""Time source, injected so rate limiting and backoff are testable without sleeping."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds since the epoch."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


def utc_datetime(clock: Clock) -> datetime:
    return datetime.fromtimestamp(clock.now(), tz=UTC)
