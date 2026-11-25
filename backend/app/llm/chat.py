"""ChatGateway: structured completions with fallback, repair and caching."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

from app.config import LLMSettings
from app.llm.cache import ResponseCache, cache_key
from app.llm.catalog import ModelCatalog, initial_output_mode, resolve_reasoning_effort, weaker_output_mode
from app.llm.circuit import CircuitBreaker
from app.llm.clock import Clock
from app.llm.errors import ExtractionParseError, LLMAuthError, LLMUnavailableError
from app.llm.executor import BadRequest, ModelUnavailable, RequestExecutor
from app.llm.structured import build_request, parse_structured, repair_messages, strict_json_schema
from app.llm.transport import LLMTransport
from app.llm.types import ChatMessage, ChatRequest, OutputMode, RawChat, TokenUsage

M = TypeVar("M", bound=BaseModel)
MS_PER_S = 1000


@dataclass(frozen=True, slots=True)
class ChatResult(Generic[M]):
    parsed: M
    raw_text: str
    model_used: str
    usage: TokenUsage
    latency_ms: int
    attempts: int  # requests sent (0 = served from cache)
    cached: bool
    output_mode: OutputMode
    reasoning_effort: str | None
    repaired: bool


class StructuredChat(Protocol):
    """What business code depends on (the gateway, or a test double)."""

    def complete_structured(
        self,
        messages: list[ChatMessage],
        response_model: type[M],
        *,
        purpose: str,
        prompt_version: str,
        force: bool = False,
    ) -> ChatResult[M]: ...


@dataclass(slots=True)
class _Exchange:
    """One model's answer so far: what was sent and what came back."""

    request: ChatRequest
    raw: RawChat
    attempts: int
    usage: TokenUsage


class ChatGateway:
    def __init__(
        self,
        *,
        transport: LLMTransport,
        catalog: ModelCatalog,
        executor: RequestExecutor,
        breaker: CircuitBreaker,
        cache: ResponseCache,
        clock: Clock,
        settings: LLMSettings,
    ) -> None:
        self._transport = transport
        self._catalog = catalog
        self._executor = executor
        self._breaker = breaker
        self._cache = cache
        self._clock = clock
        self._settings = settings

    def complete_structured(
        self,
        messages: list[ChatMessage],
        response_model: type[M],
        *,
        purpose: str,
        prompt_version: str,
        force: bool = False,
    ) -> ChatResult[M]:
        """Ask for `response_model`-shaped JSON; always validated with Pydantic.

        Models are tried in configured order (our fallback, not OpenRouter's,
        so retries, breakers and logs stay ours). A validation failure gets
        ONE repair call on the same model, then ExtractionParseError.
        """
        schema = strict_json_schema(response_model)
        key = cache_key(
            purpose,
            "|".join(self._settings.chat_models),
            prompt_version,
            {"messages": [m.to_dict() for m in messages], "schema": schema},
        )
        if not force and (cached := self._from_cache(key, response_model)) is not None:
            return cached
        if not self._settings.openrouter_api_key.get_secret_value():
            raise LLMAuthError("OPENROUTER_API_KEY is not set")

        failures: dict[str, str] = {}
        for model in self._settings.chat_models:
            if self._breaker.is_open(model):
                failures[model] = "circuit open"
                continue
            try:
                result = self._complete_with(model, messages, response_model, schema, purpose)
            except ModelUnavailable as exc:
                self._breaker.record_failure(model)
                failures[model] = str(exc)
                continue
            self._breaker.record_success(model)
            self._cache.set(key, _serialize(result))
            return result
        raise LLMUnavailableError(failures)

    def _complete_with(
        self,
        model: str,
        messages: list[ChatMessage],
        response_model: type[M],
        schema: dict[str, Any],
        purpose: str,
    ) -> ChatResult[M]:
        started = self._clock.now()
        info = self._catalog.get(model)
        effort = resolve_reasoning_effort(self._settings.llm_reasoning_effort, info)
        mode, exchange = self._send_with_mode_fallback(
            model, messages, initial_output_mode(info), schema, response_model.__name__, effort, purpose
        )
        repaired = False
        try:
            parsed = parse_structured(exchange.raw.content, response_model)
        except ValueError as first_error:
            repaired = True
            repair_request = ChatRequest(
                model=model,
                messages=repair_messages(exchange.request.messages, exchange.raw.content, str(first_error)),
                max_tokens=exchange.request.max_tokens,
                response_format=exchange.request.response_format,
                extra_body=exchange.request.extra_body,
            )
            try:
                raw, attempts = self._executor.run(
                    model, partial(self._transport.chat, repair_request), purpose=f"{purpose}:repair"
                )
            except BadRequest as rejected:
                raise ExtractionParseError(str(first_error), exchange.raw.content) from rejected
            exchange = _Exchange(
                repair_request, raw, exchange.attempts + attempts, exchange.usage + raw.usage
            )
            try:
                parsed = parse_structured(raw.content, response_model)
            except ValueError as second_error:
                raise ExtractionParseError(str(second_error), raw.content) from second_error
        return ChatResult(
            parsed=parsed,
            raw_text=exchange.raw.content,
            model_used=model,
            usage=exchange.usage,
            latency_ms=int((self._clock.now() - started) * MS_PER_S),
            attempts=exchange.attempts,
            cached=False,
            output_mode=mode,
            reasoning_effort=effort,
            repaired=repaired,
        )

    def _send_with_mode_fallback(
        self,
        model: str,
        messages: list[ChatMessage],
        mode: OutputMode,
        schema: dict[str, Any],
        schema_name: str,
        effort: str | None,
        purpose: str,
    ) -> tuple[OutputMode, _Exchange]:
        """A 400 on a structured mode drops to the next weaker mode, then gives up."""
        attempts = 0
        current: OutputMode | None = mode
        while current is not None:
            request = build_request(
                model=model,
                messages=messages,
                mode=current,
                schema=schema,
                schema_name=schema_name,
                reasoning_effort=effort,
                max_tokens=self._settings.llm_max_tokens,
            )
            try:
                raw, used = self._executor.run(model, partial(self._transport.chat, request), purpose=purpose)
            except BadRequest:
                attempts += 1
                current = weaker_output_mode(current)
                continue
            return current, _Exchange(request, raw, attempts + used, raw.usage)
        raise ModelUnavailable(f"{model}: rejected every output mode")

    def _from_cache(self, key: str, response_model: type[M]) -> ChatResult[M] | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        data = json.loads(hit)
        return ChatResult(
            parsed=response_model.model_validate(data["parsed"]),
            raw_text=data["raw_text"],
            model_used=data["model_used"],
            usage=TokenUsage(**data["usage"]),
            latency_ms=0,
            attempts=0,
            cached=True,
            output_mode=OutputMode(data["output_mode"]),
            reasoning_effort=data["reasoning_effort"],
            repaired=data["repaired"],
        )


def _serialize(result: ChatResult[Any]) -> str:
    return json.dumps(
        {
            "parsed": result.parsed.model_dump(mode="json"),
            "raw_text": result.raw_text,
            "model_used": result.model_used,
            "usage": result.usage.to_dict(),
            "output_mode": result.output_mode.value,
            "reasoning_effort": result.reasoning_effort,
            "repaired": result.repaired,
        }
    )
