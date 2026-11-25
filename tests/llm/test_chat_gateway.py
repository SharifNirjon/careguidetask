"""ChatGateway behaviour against respx-mocked OpenRouter (no network)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.llm.errors import ExtractionParseError, LLMAuthError, LLMUnavailableError, QuotaExhaustedError
from app.llm.types import ChatMessage, OutputMode
from tests.support.llm import (
    FALLBACK,
    PRIMARY,
    TEST_API_KEY,
    GatewayKit,
    chat_body,
    chat_model,
    make_kit,
    sent_json,
)

GOOD = '{"value": "INV-1", "confidence": 0.9}'
MESSAGES = [ChatMessage("system", "Extract."), ChatMessage("user", "<document>INV-1</document>")]


class Answer(BaseModel):
    value: str | None
    confidence: float


def complete(kit: GatewayKit, **kw: object):
    return kit.gateways.chat.complete_structured(MESSAGES, Answer, purpose="test", prompt_version="v1", **kw)


def models_called(router: respx.MockRouter, since: int = 0) -> list[str]:
    return [sent_json(call)["model"] for call in list(router.calls)[since:]]


def test_happy_path_sends_temperature_zero_and_strict_schema(
    kit: GatewayKit, router: respx.MockRouter
) -> None:
    router.post("/chat/completions").respond(200, json=chat_body(GOOD))
    result = complete(kit)
    body = sent_json(router.calls[0])
    assert result.parsed == Answer(value="INV-1", confidence=0.9)
    assert (result.model_used, result.attempts, result.cached) == (PRIMARY, 1, False)
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert body["provider"] == {"require_parameters": True}


def test_reasoning_effort_mapped_to_supported_and_excluded(router: respx.MockRouter) -> None:
    kit = make_kit(models=[chat_model(PRIMARY, efforts=("high", "medium", "none")), chat_model(FALLBACK)])
    router.post("/chat/completions").respond(200, json=chat_body(GOOD))
    result = complete(kit)
    assert sent_json(router.calls[0])["reasoning"] == {"effort": "medium", "exclude": True}
    assert result.reasoning_effort == "medium"


def test_429_retry_after_is_honored(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"message": "slow down"}}),
            httpx.Response(200, json=chat_body(GOOD)),
        ]
    )
    result = complete(kit)
    assert 7 in kit.clock.sleeps
    assert result.attempts == 2 and result.model_used == PRIMARY


def test_daily_cap_429_marks_quota_and_raises(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/chat/completions").respond(
        429, json={"error": {"message": "Rate limit exceeded: free-models-per-day"}}
    )
    with pytest.raises(QuotaExhaustedError):
        complete(kit)
    assert kit.gateways.quota.status().remaining == 0
    with pytest.raises(QuotaExhaustedError):
        complete(kit, force=True)
    assert router.calls.call_count == 1  # second call never left the process


def test_5xx_backs_off_then_falls_back(kit: GatewayKit, router: respx.MockRouter) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        return httpx.Response(503) if model == PRIMARY else httpx.Response(200, json=chat_body(GOOD))

    router.post("/chat/completions").mock(side_effect=respond)
    result = complete(kit)
    assert models_called(router) == [PRIMARY] * 3 + [FALLBACK]
    assert len(kit.clock.sleeps) == 2  # backoff between the 3 primary attempts
    assert result.model_used == FALLBACK


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_errors_are_permanent(kit: GatewayKit, router: respx.MockRouter, status: int) -> None:
    router.post("/chat/completions").respond(status, json={"error": {"message": "no"}})
    with pytest.raises(LLMAuthError):
        complete(kit)
    assert router.calls.call_count == 1  # no retry, no fallback


def test_schema_mode_rejected_falls_back_to_json_object(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "response_format json_schema not supported"}}),
            httpx.Response(200, json=chat_body(GOOD)),
        ]
    )
    result = complete(kit)
    second = sent_json(router.calls[1])
    assert second["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in second["messages"][0]["content"]
    assert result.output_mode is OutputMode.JSON_OBJECT


def test_model_without_response_format_starts_in_prompt_mode(router: respx.MockRouter) -> None:
    kit = make_kit(models=[chat_model(PRIMARY, structured=False), chat_model(FALLBACK)])
    router.post("/chat/completions").respond(200, json=chat_body(GOOD))
    result = complete(kit)
    body = sent_json(router.calls[0])
    assert "response_format" not in body and "provider" not in body
    assert '"additionalProperties": false' in body["messages"][0]["content"]
    assert result.output_mode is OutputMode.PROMPT and router.calls.call_count == 1


def test_malformed_json_is_repaired_once(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=chat_body('{"value": "INV-1"')),
            httpx.Response(200, json=chat_body(GOOD)),
        ]
    )
    result = complete(kit)
    repair = sent_json(router.calls[1])["messages"]
    assert repair[-2] == {"role": "assistant", "content": '{"value": "INV-1"'}
    assert "failed validation" in repair[-1]["content"]
    assert result.repaired and result.attempts == 2


def test_failed_repair_raises_parse_error(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/chat/completions").respond(200, json=chat_body('{"value": 5}'))
    with pytest.raises(ExtractionParseError) as info:
        complete(kit)
    assert "confidence" in info.value.errors
    assert router.calls.call_count == 2  # original + exactly one repair


def test_think_blocks_and_fences_are_stripped(kit: GatewayKit, router: respx.MockRouter) -> None:
    wrapped = f"<think>The invoice number is probably INV-2.</think>\n```json\n{GOOD}\n```"
    router.post("/chat/completions").respond(200, json=chat_body(wrapped))
    assert complete(kit).parsed.value == "INV-1"


def test_circuit_breaker_opens_and_closes(kit: GatewayKit, router: respx.MockRouter) -> None:
    primary_up = {"value": False}

    def respond(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == PRIMARY and not primary_up["value"]:
            return httpx.Response(500)
        return httpx.Response(200, json=chat_body(GOOD))

    router.post("/chat/completions").mock(side_effect=respond)
    for _ in range(kit.settings.circuit_failure_threshold):
        complete(kit, force=True)
    assert models_called(router).count(PRIMARY) == 3 * kit.settings.circuit_failure_threshold

    mark = len(router.calls)
    complete(kit, force=True)
    assert models_called(router, mark) == [FALLBACK]  # open: primary skipped entirely

    kit.clock.advance(kit.settings.circuit_open_s + 1)
    primary_up["value"] = True
    assert complete(kit, force=True).model_used == PRIMARY  # trial call succeeds -> closed
    mark = len(router.calls)
    complete(kit, force=True)
    assert models_called(router, mark) == [PRIMARY]


def test_all_models_down_raises_unavailable(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/chat/completions").respond(502)
    with pytest.raises(LLMUnavailableError) as info:
        complete(kit)
    assert set(info.value.failures) == {PRIMARY, FALLBACK}


def test_exhausted_quota_raises_without_sending(kit: GatewayKit, router: respx.MockRouter) -> None:
    route = router.post("/chat/completions").respond(200, json=chat_body(GOOD))
    for _ in range(kit.settings.free_model_daily_limit):
        kit.gateways.quota.reserve()
    with pytest.raises(QuotaExhaustedError) as info:
        complete(kit)
    assert route.call_count == 0
    assert info.value.resets_at.hour == 0  # next UTC midnight


def test_cache_hit_sends_no_request(kit: GatewayKit, router: respx.MockRouter) -> None:
    route = router.post("/chat/completions").respond(200, json=chat_body(GOOD))
    first = complete(kit)
    second = complete(kit)
    assert route.call_count == 1
    assert second.cached and second.attempts == 0 and second.parsed == first.parsed
    complete(kit, force=True)
    assert route.call_count == 2


def test_prompt_version_changes_the_cache_key(kit: GatewayKit, router: respx.MockRouter) -> None:
    route = router.post("/chat/completions").respond(200, json=chat_body(GOOD))
    complete(kit)
    kit.gateways.chat.complete_structured(MESSAGES, Answer, purpose="test", prompt_version="v2")
    assert route.call_count == 2


def test_api_key_never_logged(
    kit: GatewayKit, router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    router.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(401, json={"error": {"message": "bad key"}}),
        ]
    )
    with pytest.raises(LLMAuthError):
        complete(kit)
    assert router.calls[0].request.headers["authorization"] == f"Bearer {TEST_API_KEY}"
    assert TEST_API_KEY not in caplog.text
    assert TEST_API_KEY[-12:] not in caplog.text
    assert "INV-1" not in caplog.text  # no document text in logs either
    assert TEST_API_KEY not in repr(kit.settings)


def test_missing_api_key_fails_before_any_request(router: respx.MockRouter) -> None:
    from pydantic import SecretStr

    kit = make_kit(openrouter_api_key=SecretStr(""))
    route = router.post("/chat/completions").respond(200, json=chat_body(GOOD))
    with pytest.raises(LLMAuthError, match="not set"):
        complete(kit)
    assert route.call_count == 0
