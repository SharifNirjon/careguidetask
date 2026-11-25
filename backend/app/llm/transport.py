"""OpenRouter edge: the ONLY module that imports the openai SDK or talks HTTP.

Everything is translated into app.llm.types: callers never see SDK objects,
and every failure arrives as a classified ProviderError.
"""

from __future__ import annotations

import email.utils
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import partial
from typing import Any, Protocol

import httpx
import openai
from pydantic import SecretStr

from app.llm.budget import QuotaStatus
from app.llm.types import (
    ChatRequest,
    EmbedRequest,
    ModelInfo,
    ProviderError,
    ProviderErrorKind,
    RawChat,
    RawEmbedding,
    TokenUsage,
)

log = logging.getLogger(__name__)
# The SDK logs full request bodies (document text) at DEBUG and httpx logs every
# URL at INFO. Pin both here, at the edge, so no global log level can leak them.
for _noisy in ("openai", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

MS_PER_S = 1000
AUTH_STATUSES = frozenset({401, 402, 403})


class LLMTransport(Protocol):
    def chat(self, request: ChatRequest) -> RawChat: ...

    def embed(self, request: EmbedRequest) -> RawEmbedding: ...


def parse_retry_after(headers: Mapping[str, str], now: float) -> float | None:
    """Retry-After as seconds (numeric or HTTP-date)."""
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(value)
        return None if parsed is None else max(parsed.timestamp() - now, 0.0)


def parse_reset_at(headers: Mapping[str, str]) -> float | None:
    """X-RateLimit-Reset is epoch milliseconds on OpenRouter."""
    value = headers.get("x-ratelimit-reset")
    try:
        return float(value) / MS_PER_S if value else None
    except ValueError:
        return None


def classify_status(status: int) -> ProviderErrorKind:
    if status == 429:
        return ProviderErrorKind.RATE_LIMITED
    if status in AUTH_STATUSES:
        return ProviderErrorKind.AUTH
    if status >= 500:
        return ProviderErrorKind.SERVER
    return ProviderErrorKind.BAD_REQUEST


def _error_message(body: object) -> str:
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return str(error.get("message", ""))[:300]
    return ""


@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    except openai.APIStatusError as exc:
        headers = {k.lower(): v for k, v in exc.response.headers.items()}
        raise ProviderError(
            classify_status(exc.status_code),
            status=exc.status_code,
            message=_error_message(exc.body),
            retry_after_s=parse_retry_after(headers, time.time()),
            reset_at_s=parse_reset_at(headers),
        ) from exc
    except openai.APITimeoutError as exc:
        raise ProviderError(ProviderErrorKind.TIMEOUT) from exc
    except openai.APIConnectionError as exc:
        raise ProviderError(ProviderErrorKind.CONNECTION) from exc


def _usage(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    details = getattr(usage, "completion_tokens_details", None)
    return TokenUsage(
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        reasoning_tokens=int(getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
    )


class OpenRouterTransport:
    """openai SDK pointed at OpenRouter. SDK retries are off: the gateway owns them."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        connect_timeout_s: float,
        read_timeout_s: float,
        client_factory: Callable[..., openai.OpenAI] = openai.OpenAI,
    ) -> None:
        self._make_client = partial(
            client_factory,
            base_url=base_url,
            max_retries=0,
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
        )
        self._api_key = api_key
        self._sdk: openai.OpenAI | None = None

    @property
    def _client(self) -> openai.OpenAI:
        """Built on first use: the SDK refuses an empty key, and gateways check
        for a missing key (with a clear error) before any request."""
        if self._sdk is None:
            self._sdk = self._make_client(api_key=self._api_key.get_secret_value())
        return self._sdk

    def chat(self, request: ChatRequest) -> RawChat:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [m.to_dict() for m in request.messages],
            "temperature": 0,
            "max_tokens": request.max_tokens,
            "extra_body": request.extra_body,
        }
        if request.response_format is not None:
            kwargs["response_format"] = request.response_format
        with _translate_errors():
            response = self._client.chat.completions.create(**kwargs)
        if not response.choices:  # OpenRouter can return 200 with an error body
            raise ProviderError(ProviderErrorKind.SERVER, status=200, message="no choices in response")
        choice = response.choices[0]
        return RawChat(
            content=choice.message.content or "",
            finish_reason=choice.finish_reason,
            usage=_usage(response.usage),
        )

    def embed(self, request: EmbedRequest) -> RawEmbedding:
        with _translate_errors():
            response = self._client.embeddings.create(
                model=request.model,
                input=request.inputs,
                dimensions=request.dimensions,
                encoding_format="float",
                extra_body={"input_type": request.input_type},
            )
        if not response.data:
            raise ProviderError(ProviderErrorKind.SERVER, status=200, message="no embeddings in response")
        ordered = sorted(response.data, key=lambda item: item.index)
        return RawEmbedding(vectors=[list(item.embedding) for item in ordered], usage=_usage(response.usage))


class OpenRouterMetadata:
    """Public model catalog and the key's free-tier counter. Neither costs request quota."""

    def __init__(
        self, *, api_key: SecretStr, base_url: str, timeout_s: float, client: httpx.Client | None = None
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)

    def list_models(self) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        for path in ("/models", "/embeddings/models"):
            response = self._client.get(f"{self._base_url}{path}")
            response.raise_for_status()
            models.extend(model_info(item) for item in response.json().get("data", []))
        return models

    def fetch(self) -> QuotaStatus | None:
        """Satisfies QuotaSource: OpenRouter's own free-model daily counter."""
        try:
            response = self._client.get(
                f"{self._base_url}/key",
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            log.warning("quota resync failed", extra={"outcome": "unavailable"})
            return None
        daily = response.json().get("data", {}).get("free_model_daily_requests")
        if not isinstance(daily, dict):
            return None
        return QuotaStatus(used=int(daily.get("used", 0)), limit=int(daily.get("limit", 0)))


def model_info(item: Mapping[str, Any]) -> ModelInfo:
    reasoning = item.get("reasoning") or {}
    efforts = reasoning.get("supported_efforts") if isinstance(reasoning, dict) else None
    return ModelInfo(
        id=str(item["id"]),
        supported_parameters=frozenset(item.get("supported_parameters") or ()),
        reasoning_efforts=tuple(efforts) if efforts else None,
        context_length=item.get("context_length"),
    )
