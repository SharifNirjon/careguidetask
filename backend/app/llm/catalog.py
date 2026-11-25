"""Model capabilities, so the gateway never spends quota discovering them by trial.

The free text model, for instance, supports `reasoning` but not
`response_format`; asking for json_schema would burn a request on a 400.
"""

from __future__ import annotations

import json
from typing import Protocol

from redis import Redis

from app.llm.types import ModelInfo, OutputMode

CACHE_KEY = "llm:catalog"
# Ordered weakest -> strongest; used to map a requested effort to a supported one.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class ModelCatalog(Protocol):
    def get(self, model: str) -> ModelInfo | None: ...


class ModelListSource(Protocol):
    def list_models(self) -> list[ModelInfo]: ...


class CachedModelCatalog:
    """Fetches the public catalog at most once per TTL across all workers."""

    def __init__(self, source: ModelListSource, redis: Redis, *, ttl_s: int) -> None:
        self._source = source
        self._redis = redis
        self._ttl_s = ttl_s

    def get(self, model: str) -> ModelInfo | None:
        raw = self._redis.hget(CACHE_KEY, model)
        if raw is None and not self._redis.exists(CACHE_KEY):
            self._refresh()
            raw = self._redis.hget(CACHE_KEY, model)
        return None if raw is None else _decode(raw)

    def _refresh(self) -> None:
        models = self._source.list_models()
        if not models:
            return
        self._redis.hset(CACHE_KEY, mapping={m.id: _encode(m) for m in models})
        self._redis.expire(CACHE_KEY, self._ttl_s)


def _encode(info: ModelInfo) -> str:
    return json.dumps(
        {
            "id": info.id,
            "supported_parameters": sorted(info.supported_parameters),
            "reasoning_efforts": list(info.reasoning_efforts) if info.reasoning_efforts else None,
            "context_length": info.context_length,
        }
    )


def _decode(raw: bytes | str) -> ModelInfo:
    data = json.loads(raw)
    efforts = data.get("reasoning_efforts")
    return ModelInfo(
        id=data["id"],
        supported_parameters=frozenset(data["supported_parameters"]),
        reasoning_efforts=tuple(efforts) if efforts else None,
        context_length=data.get("context_length"),
    )


def initial_output_mode(info: ModelInfo | None) -> OutputMode:
    """Strongest structured-output mode the model advertises (unknown: try the strongest)."""
    if info is None or "structured_outputs" in info.supported_parameters:
        return OutputMode.JSON_SCHEMA
    if "response_format" in info.supported_parameters:
        return OutputMode.JSON_OBJECT
    return OutputMode.PROMPT


def weaker_output_mode(mode: OutputMode) -> OutputMode | None:
    """Next mode after the provider rejected `mode` with a 400."""
    order = list(OutputMode)
    index = order.index(mode)
    return order[index + 1] if index + 1 < len(order) else None


def resolve_reasoning_effort(requested: str, info: ModelInfo | None) -> str | None:
    """Nearest supported effort to `requested` (ties go to the stronger one).

    None means "don't send an effort": the model doesn't expose the knob.
    """
    if info is None or info.reasoning_efforts is None:
        return None
    supported = [e for e in info.reasoning_efforts if e in EFFORT_ORDER]
    if not supported:
        return None
    if requested in supported:
        return requested
    target = EFFORT_ORDER.index(requested) if requested in EFFORT_ORDER else EFFORT_ORDER.index("low")
    return min(supported, key=lambda e: (abs(EFFORT_ORDER.index(e) - target), -EFFORT_ORDER.index(e)))
