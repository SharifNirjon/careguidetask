"""Response cache: identical (purpose, model, prompt version, input) costs no quota."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from redis import Redis

KEY_PREFIX = "llm:cache"


class ResponseCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


def cache_key(purpose: str, model: str, prompt_version: str, payload: object) -> str:
    """Stable key; any change to the prompt version or input changes it."""
    body = json.dumps([purpose, model, prompt_version, payload], sort_keys=True, ensure_ascii=False)
    return f"{KEY_PREFIX}:{hashlib.sha256(body.encode()).hexdigest()}"


class RedisResponseCache:
    def __init__(self, redis: Redis, *, ttl_s: int) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    def get(self, key: str) -> str | None:
        value = self._redis.get(key)
        return value.decode() if isinstance(value, bytes) else value

    def set(self, key: str, value: str) -> None:
        self._redis.set(key, value, ex=self._ttl_s)
