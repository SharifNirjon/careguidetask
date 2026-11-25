"""Pure helpers: keys, cursors, If-Match, progress, snippets, filenames."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.api.auth import bearer_token, generate_key, prefix_of, verify
from app.api.errors import PreconditionFailed, PreconditionRequired, ValidationFailed
from app.api.pagination import decode_cursor, encode_cursor
from app.api.routers.documents import content_disposition
from app.api.services.corrections import parse_if_match
from app.api.services.progress import EventView, JobView, compute_progress
from app.models.enums import EventStatus, JobStatus, JobType, Stage

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_generated_key_round_trip() -> None:
    generated = generate_key()
    assert prefix_of(generated.key) == generated.prefix and generated.prefix.startswith("cg_")
    assert verify(generated.key, generated.key_hash)
    assert not verify(generated.key + "x", generated.key_hash)
    assert generated.key not in generated.key_hash


@pytest.mark.parametrize(
    ("header", "token"),
    [("Bearer abc", "abc"), ("bearer abc", "abc"), ("Basic abc", None), ("Bearer ", None), (None, None)],
)
def test_bearer_token(header: str | None, token: str | None) -> None:
    assert bearer_token(header) == token


def test_cursor_round_trip_and_tampering() -> None:
    token = encode_cursor(["2026-01-01", T0, "3fa2"])
    assert decode_cursor(token, arity=3) is not None
    with pytest.raises(ValidationFailed):
        decode_cursor(token, arity=2)
    with pytest.raises(ValidationFailed):
        decode_cursor("%%%", arity=3)


@pytest.mark.parametrize(("header", "version"), [("3", 3), ('"3"', 3), ('W/"7"', 7)])
def test_if_match_parsing(header: str, version: int) -> None:
    assert parse_if_match(header) == version


def test_if_match_errors() -> None:
    with pytest.raises(PreconditionRequired):
        parse_if_match(None)
    with pytest.raises(PreconditionFailed):
        parse_if_match('"abc"')


def _job(status: JobStatus, job_type: JobType = JobType.FULL) -> JobView:
    return JobView(job_type, status, T0, T0 + timedelta(seconds=9) if status is JobStatus.SUCCEEDED else None)


def test_progress_mid_parse() -> None:
    events = [
        EventView(Stage.PARSE, EventStatus.STARTED, {}),
        EventView(Stage.PARSE, EventStatus.SUCCEEDED, {"scope": "batch", "units": 5}),
    ]
    progress = compute_progress(
        _job(JobStatus.RUNNING), events, units_total=10, now=T0 + timedelta(seconds=2)
    )
    assert (progress.stage, progress.stages_done, progress.units_done, progress.percent) == (
        Stage.PARSE,
        0,
        5,
        10,
    )
    assert progress.elapsed_ms == 2000


def test_progress_after_extract() -> None:
    events = [
        EventView(s, EventStatus.SUCCEEDED, {"scope": "document"}) for s in (Stage.PARSE, Stage.SEGMENT)
    ]
    events.append(EventView(Stage.EXTRACT, EventStatus.SUCCEEDED, {"scope": "segment"}))  # partial only
    progress = compute_progress(_job(JobStatus.RUNNING), events, units_total=1, now=T0)
    assert (progress.stage, progress.stages_done, progress.percent) == (Stage.EXTRACT, 2, 40)


def test_progress_done_and_no_job() -> None:
    done = compute_progress(_job(JobStatus.SUCCEEDED, JobType.REVALIDATE), [], units_total=None, now=T0)
    assert (done.stage, done.percent, done.elapsed_ms, done.stages_total) == (None, 100, 9000, 1)
    nothing = compute_progress(None, [], units_total=None, now=T0)
    assert nothing.percent == 0 and nothing.stage is None


def test_content_disposition_cannot_inject_headers() -> None:
    value = content_disposition('a"b\r\nSet-Cookie: x;.pdf')
    assert "\r" not in value and "\n" not in value
    assert value.startswith('inline; filename="a_b__Set-Cookie: x_.pdf"')
    assert value.endswith("filename*=UTF-8''a%22b%0D%0ASet-Cookie%3A%20x%3B.pdf")
    assert content_disposition("rëchnung.pdf").startswith('inline; filename="r_chnung.pdf"')
