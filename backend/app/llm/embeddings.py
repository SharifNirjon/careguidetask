"""EmbeddingGateway: batched, dimension-checked, space-tagged embeddings.

EMBEDDING SPACES ARE NOT INTERCHANGEABLE. Every result names its space, and
one result never mixes spaces: falling back to a model in the SAME space
reuses already-embedded batches; falling back to another space re-embeds the
whole call with that model and tags the result, so Step 7 can store and
search per space and re-embed later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import partial
from typing import Literal

from redis import Redis

from app.config import EmbeddingModelConfig, LLMSettings
from app.llm.catalog import ModelCatalog
from app.llm.circuit import CircuitBreaker
from app.llm.errors import EmbeddingDimensionError, LLMAuthError, LLMUnavailableError
from app.llm.executor import BadRequest, ModelUnavailable, RequestExecutor
from app.llm.transport import LLMTransport
from app.llm.types import EmbedRequest

InputType = Literal["query", "document"]
NATIVE_DIM_KEY = "llm:embed:native_dim"


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model_used: str  # comma-separated when same-space fallback served some batches
    space: str
    dims: int
    is_primary_space: bool
    truncated_inputs: int  # inputs cut to fit the model's context


@dataclass(frozen=True, slots=True)
class _Batch:
    model: str
    vectors: list[list[float]]
    truncated: int


def fit_dimension(vector: list[float], dim: int, model: str) -> list[float]:
    """Matryoshka-style truncation to `dim`, then L2 normalization.

    A shorter vector can't be fixed (padding would distort similarity), so
    it fails fast instead.
    """
    if len(vector) < dim:
        raise EmbeddingDimensionError(f"{model} returned {len(vector)} dims; need >= {dim}")
    head = vector[:dim]
    norm = math.sqrt(sum(v * v for v in head))
    return [v / norm for v in head] if norm > 0 else head


def batches(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def max_input_chars(context_tokens: int | None, chars_per_token: float) -> int | None:
    return int(context_tokens * chars_per_token) if context_tokens else None


class EmbeddingGateway:
    def __init__(
        self,
        *,
        transport: LLMTransport,
        catalog: ModelCatalog,
        executor: RequestExecutor,
        breaker: CircuitBreaker,
        redis: Redis,
        settings: LLMSettings,
        dim: int,
    ) -> None:
        self._transport = transport
        self._catalog = catalog
        self._executor = executor
        self._breaker = breaker
        self._redis = redis
        self._settings = settings
        self._dim = dim

    def embed(self, texts: list[str], *, input_type: InputType) -> EmbeddingResult:
        """One request per EMBED_BATCH_SIZE inputs (every request costs daily quota)."""
        if not self._settings.openrouter_api_key.get_secret_value():
            raise LLMAuthError("OPENROUTER_API_KEY is not set")
        configs = self._settings.embedding_models
        chunks = batches(texts, self._settings.embed_batch_size)
        done: dict[str, dict[int, _Batch]] = {}  # space -> batch index -> result
        failures: dict[str, str] = {}
        for config in configs:
            if self._breaker.is_open(config.model):
                failures[config.model] = "circuit open"
                continue
            space_done = done.setdefault(config.space, {})
            try:
                self._embed_missing(config, chunks, space_done, input_type)
            except (ModelUnavailable, BadRequest) as exc:
                self._breaker.record_failure(config.model)
                failures[config.model] = str(exc)
                continue
            self._breaker.record_success(config.model)
            ordered = [space_done[i] for i in range(len(chunks))]
            return EmbeddingResult(
                vectors=[v for batch in ordered for v in batch.vectors],
                model_used=",".join(dict.fromkeys(batch.model for batch in ordered)),
                space=config.space,
                dims=self._dim,
                is_primary_space=config.space == configs[0].space,
                truncated_inputs=sum(batch.truncated for batch in ordered),
            )
        raise LLMUnavailableError(failures)

    def _embed_missing(
        self,
        config: EmbeddingModelConfig,
        chunks: list[list[str]],
        done: dict[int, _Batch],
        input_type: InputType,
    ) -> None:
        info = self._catalog.get(config.model)
        limit = max_input_chars(info.context_length if info else None, self._settings.embed_chars_per_token)
        api_input_type = (
            self._settings.embed_input_type_query
            if input_type == "query"
            else self._settings.embed_input_type_document
        )
        for index, chunk in enumerate(chunks):
            if index in done:
                continue  # same-space fallback: keep what an earlier model produced
            inputs = [t[:limit] if limit else t for t in chunk]
            request = EmbedRequest(
                model=config.model, inputs=inputs, dimensions=self._dim, input_type=api_input_type
            )
            raw, _ = self._executor.run(
                config.model, partial(self._transport.embed, request), purpose="embed"
            )
            if raw.vectors:
                self._redis.hset(NATIVE_DIM_KEY, config.model, len(raw.vectors[0]))  # shown by llm-check
            done[index] = _Batch(
                model=config.model,
                vectors=[fit_dimension(v, self._dim, config.model) for v in raw.vectors],
                truncated=sum(1 for a, b in zip(chunk, inputs, strict=True) if a != b),
            )
