"""Rate limiter, daily quota and resync, under a fake clock."""

from __future__ import annotations

import fakeredis
import pytest

from app.llm.budget import DailyQuota, QuotaStatus, SlidingWindowRateLimiter, is_free_model
from app.llm.errors import QuotaExhaustedError
from tests.support.llm import FakeClock, StubMetadata

DAY_S = 24 * 3600


def test_25_calls_at_18_per_minute_make_the_excess_wait() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(fakeredis.FakeRedis(), clock, limit=18, window_s=60)
    start = clock.now()
    stamps = []
    waits = []
    for _ in range(25):
        waits.append(limiter.acquire())
        stamps.append(clock.now() - start)
    assert waits[:18] == [0.0] * 18
    assert waits[18] >= 60  # the 19th call waits for the first slots to expire
    assert all(t >= 60 for t in stamps[18:])  # none of the 7 excess calls went early
    for i, t in enumerate(stamps):  # never more than 18 calls in any 60 s window
        assert sum(1 for u in stamps if t - 60 < u <= t) <= 18, i


def test_limiter_is_shared_across_instances() -> None:
    redis, clock = fakeredis.FakeRedis(), FakeClock()
    a = SlidingWindowRateLimiter(redis, clock, limit=2, window_s=60)
    b = SlidingWindowRateLimiter(redis, clock, limit=2, window_s=60)
    assert a.acquire() == 0 and b.acquire() == 0
    assert a.acquire() > 0  # "another worker" already used the window


def _quota(clock: FakeClock, source: StubMetadata | None = None, limit: int = 3) -> DailyQuota:
    return DailyQuota(fakeredis.FakeRedis(), clock, default_limit=limit, source=source, resync_s=300)


def test_daily_quota_stops_before_the_limit_and_resets_at_utc_midnight() -> None:
    clock = FakeClock()
    quota = _quota(clock)
    for _ in range(3):
        quota.reserve()
    with pytest.raises(QuotaExhaustedError) as info:
        quota.reserve()
    assert quota.status() == QuotaStatus(used=3, limit=3)  # the refused request isn't counted
    assert info.value.resets_at.timestamp() > clock.now()
    clock.advance(DAY_S)
    quota.reserve()  # a new UTC day, a new budget


def test_resync_adopts_provider_counter_but_never_lowers_ours() -> None:
    clock = FakeClock()
    source = StubMetadata(remote_quota=QuotaStatus(used=40, limit=1000))
    quota = _quota(clock, source)
    assert quota.status() == QuotaStatus(used=40, limit=1000)
    source.remote_quota = QuotaStatus(used=10, limit=1000)
    clock.advance(301)
    assert quota.status().used == 40


def test_mark_exhausted() -> None:
    quota = _quota(FakeClock(), limit=50)
    quota.mark_exhausted()
    assert quota.status().remaining == 0


@pytest.mark.parametrize(("model", "free"), [("a/b:free", True), ("a/b", False), ("a/free-b", False)])
def test_free_model_detection(model: str, free: bool) -> None:
    assert is_free_model(model) is free
