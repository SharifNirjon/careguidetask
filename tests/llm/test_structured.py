"""Pure helpers: strict schema, output cleaning, mode/effort selection, header parsing."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from app.llm.catalog import initial_output_mode, resolve_reasoning_effort, weaker_output_mode
from app.llm.executor import backoff_delay, is_daily_cap, rate_limit_wait
from app.llm.structured import clean_model_text, parse_structured, strict_json_schema
from app.llm.transport import classify_status, parse_reset_at, parse_retry_after
from app.llm.types import ModelInfo, OutputMode, ProviderError, ProviderErrorKind


class Inner(BaseModel):
    text: str | None
    score: float = Field(ge=0, le=1)


class Outer(BaseModel):
    kind: str
    items: list[Inner]
    note: str | None = None


def test_strict_schema_requires_everything_and_forbids_extras() -> None:
    schema = strict_json_schema(Outer)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["kind", "items", "note"]
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False and inner["required"] == ["text", "score"]
    assert "minimum" not in str(schema) and "default" not in str(schema)
    assert {"type": "null"} in inner["properties"]["text"]["anyOf"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('<think>hmm {"a": 2}</think>{"a": 1}', '{"a": 1}'),
        ('reasoning... </think>\n{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('Here you go: {"a": 1} hope it helps', '{"a": 1}'),
    ],
)
def test_clean_model_text(raw: str, expected: str) -> None:
    assert clean_model_text(raw) == expected


def test_parse_structured_reports_readable_errors() -> None:
    with pytest.raises(ValueError, match=r"items\.0\.score"):
        parse_structured('{"kind": "x", "items": [{"text": null, "score": 3}], "note": null}', Outer)


@pytest.mark.parametrize(
    ("params", "mode"),
    [
        ({"structured_outputs", "response_format"}, OutputMode.JSON_SCHEMA),
        ({"response_format"}, OutputMode.JSON_OBJECT),
        ({"reasoning", "tools"}, OutputMode.PROMPT),
    ],
)
def test_initial_output_mode(params: set[str], mode: OutputMode) -> None:
    assert initial_output_mode(ModelInfo(id="m", supported_parameters=frozenset(params))) is mode


def test_output_mode_ladder() -> None:
    assert initial_output_mode(None) is OutputMode.JSON_SCHEMA
    assert weaker_output_mode(OutputMode.JSON_SCHEMA) is OutputMode.JSON_OBJECT
    assert weaker_output_mode(OutputMode.JSON_OBJECT) is OutputMode.PROMPT
    assert weaker_output_mode(OutputMode.PROMPT) is None


@pytest.mark.parametrize(
    ("requested", "supported", "expected"),
    [
        ("low", ("high", "medium", "none"), "medium"),  # the real free Nemotron model
        ("low", ("low", "high"), "low"),
        ("high", ("high", "medium", "none"), "high"),
        ("minimal", ("high", "medium", "none"), "none"),
        ("low", None, None),
    ],
)
def test_reasoning_effort_resolution(
    requested: str, supported: tuple[str, ...] | None, expected: str | None
) -> None:
    assert resolve_reasoning_effort(requested, ModelInfo(id="m", reasoning_efforts=supported)) == expected


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, ProviderErrorKind.RATE_LIMITED),
        (401, ProviderErrorKind.AUTH),
        (402, ProviderErrorKind.AUTH),
        (403, ProviderErrorKind.AUTH),
        (500, ProviderErrorKind.SERVER),
        (503, ProviderErrorKind.SERVER),
        (400, ProviderErrorKind.BAD_REQUEST),
        (422, ProviderErrorKind.BAD_REQUEST),
    ],
)
def test_status_classification(status: int, kind: ProviderErrorKind) -> None:
    assert classify_status(status) is kind


def test_header_parsing() -> None:
    now = 1_700_000_000.0
    assert parse_retry_after({"retry-after": "12"}, now) == 12
    assert parse_retry_after({"retry-after": "Tue, 14 Nov 2023 22:13:30 GMT"}, now) == pytest.approx(
        10, abs=1
    )
    assert parse_retry_after({}, now) is None
    assert parse_reset_at({"x-ratelimit-reset": "1700000060000"}) == 1_700_000_060.0


def test_rate_limit_wait_and_daily_cap_detection() -> None:
    now = 1_000.0
    minute = ProviderError(ProviderErrorKind.RATE_LIMITED, reset_at_s=now + 20)
    day = ProviderError(ProviderErrorKind.RATE_LIMITED, reset_at_s=now + 8 * 3600)
    assert rate_limit_wait(minute, now, default_s=2) == 20
    assert not is_daily_cap(minute, now, 60) and is_daily_cap(day, now, 60)
    assert is_daily_cap(ProviderError(ProviderErrorKind.RATE_LIMITED, message="free-models-per-day"), now, 60)


def test_backoff_grows_and_is_capped() -> None:
    import random

    rng = random.Random(0)
    delays = [max(backoff_delay(a, base_s=1, max_s=8, rng=rng) for _ in range(200)) for a in (1, 2, 3, 6)]
    assert delays[0] <= 1 < delays[1] <= 2 < delays[2] <= 4 and delays[3] <= 8
