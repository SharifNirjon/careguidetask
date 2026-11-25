"""Deterministic test doubles for the LLM gateway: clock, catalog, quota source,
and helpers that build real gateways against respx-mocked OpenRouter HTTP."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import fakeredis
from pydantic import SecretStr

from app.config import EMBEDDING_DIM, EmbeddingModelConfig, LLMSettings
from app.llm.budget import QuotaStatus
from app.llm.factory import Gateways, build_gateways
from app.llm.types import ModelInfo

BASE_URL = "https://openrouter.test/api/v1"
TEST_API_KEY = "sk-or-v1-test-0000000000000000000000000000000000000000"  # pragma: allowlist secret
PRIMARY = "vendor/primary:free"
FALLBACK = "vendor/fallback:free"
EMBED_PRIMARY = "vendor/embed-big:free"
EMBED_SAME_SPACE = "vendor/embed-big-mirror:free"
EMBED_OTHER = "vendor/embed-small:free"
# Real, indexed space names (LLMSettings rejects spaces without an HNSW index).
SPACE_BIG = "nemotron-3.5-8b"
SPACE_SMALL = "nemotron-3-1b"
START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC).timestamp()


class FakeClock:
    """Time only moves when someone sleeps; every sleep is recorded."""

    def __init__(self, start: float = START) -> None:
        self.current = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += max(seconds, 0.0)

    def advance(self, seconds: float) -> None:
        self.current += seconds


@dataclass
class StubMetadata:
    """Satisfies ModelListSource + QuotaSource without HTTP."""

    models: list[ModelInfo] = field(default_factory=list)
    remote_quota: QuotaStatus | None = None

    def list_models(self) -> list[ModelInfo]:
        return self.models

    def fetch(self) -> QuotaStatus | None:
        return self.remote_quota


def chat_model(
    model_id: str, *, structured: bool = True, efforts: tuple[str, ...] | None = None
) -> ModelInfo:
    params = {"temperature", "max_tokens", "reasoning"}
    if structured:
        params |= {"structured_outputs", "response_format"}
    return ModelInfo(
        id=model_id, supported_parameters=frozenset(params), reasoning_efforts=efforts, context_length=200_000
    )


def embed_model(model_id: str, context: int = 4096) -> ModelInfo:
    return ModelInfo(id=model_id, context_length=context)


def llm_test_settings(**overrides: Any) -> LLMSettings:
    values: dict[str, Any] = {
        "openrouter_api_key": SecretStr(TEST_API_KEY),
        "openrouter_base_url": BASE_URL,
        "llm_primary_model": PRIMARY,
        "llm_fallback_models": [FALLBACK],
        "embedding_models": [
            EmbeddingModelConfig(model=EMBED_PRIMARY, space=SPACE_BIG),
            EmbeddingModelConfig(model=EMBED_SAME_SPACE, space=SPACE_BIG),
            EmbeddingModelConfig(model=EMBED_OTHER, space=SPACE_SMALL),
        ],
        "llm_backoff_base_s": 1.0,
        "free_model_daily_limit": 50,
        "quota_resync_s": 10**9,
    }
    values.update(overrides)
    return LLMSettings(**values)


@dataclass
class GatewayKit:
    gateways: Gateways
    clock: FakeClock
    redis: fakeredis.FakeRedis
    metadata: StubMetadata
    settings: LLMSettings


def make_kit(*, models: list[ModelInfo] | None = None, **setting_overrides: Any) -> GatewayKit:
    clock = FakeClock()
    redis = fakeredis.FakeRedis()
    metadata = StubMetadata(
        models=models
        if models is not None
        else [
            chat_model(PRIMARY),
            chat_model(FALLBACK),
            embed_model(EMBED_PRIMARY),
            embed_model(EMBED_SAME_SPACE),
            embed_model(EMBED_OTHER),
        ]
    )
    settings = llm_test_settings(**setting_overrides)
    gateways = build_gateways(settings, redis, clock=clock, metadata=metadata)  # type: ignore[arg-type]
    return GatewayKit(gateways, clock, redis, metadata, settings)


def chat_body(content: str, *, prompt: int = 100, completion: int = 50) -> dict[str, Any]:
    return {
        "id": "gen-test",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def embed_body(n: int, dim: int = EMBEDDING_DIM * 4, value: float = 0.5) -> dict[str, Any]:
    return {
        "object": "list",
        "model": "m",
        "data": [{"object": "embedding", "index": i, "embedding": [value] * dim} for i in range(n)],
        "usage": {"prompt_tokens": n, "total_tokens": n},
    }


def sent_json(call: Any) -> dict[str, Any]:
    return json.loads(call.request.content)
