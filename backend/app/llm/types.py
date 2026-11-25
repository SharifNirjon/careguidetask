"""Provider-neutral request/response types. No SDK types leave app/llm."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


class OutputMode(StrEnum):
    """How structure is requested, best first. Validation happens in every mode."""

    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    PROMPT = "prompt"


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    max_tokens: int
    response_format: dict[str, Any] | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawChat:
    content: str
    finish_reason: str | None
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class EmbedRequest:
    model: str
    inputs: list[str]
    dimensions: int
    input_type: str


@dataclass(frozen=True, slots=True)
class RawEmbedding:
    vectors: list[list[float]]
    usage: TokenUsage


class ProviderErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    SERVER = "server"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    BAD_REQUEST = "bad_request"
    AUTH = "auth"


class ProviderError(Exception):
    """A failed provider call, already classified. Raised by transports only."""

    def __init__(
        self,
        kind: ProviderErrorKind,
        *,
        status: int | None = None,
        message: str = "",
        retry_after_s: float | None = None,
        reset_at_s: float | None = None,
    ) -> None:
        super().__init__(f"{kind}: status={status} {message}".strip())
        self.kind = kind
        self.status = status
        self.message = message
        self.retry_after_s = retry_after_s
        self.reset_at_s = reset_at_s  # epoch seconds, from X-RateLimit-Reset


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    supported_parameters: frozenset[str] = frozenset()
    reasoning_efforts: tuple[str, ...] | None = None  # None: effort not selectable
    context_length: int | None = None
