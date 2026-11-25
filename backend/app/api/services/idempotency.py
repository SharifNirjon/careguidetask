"""Idempotency-Key for uploads, stored in Redis.

begin() claims the key with a short-lived "pending" marker (SET NX), so two
concurrent replays can't both ingest; complete() replaces it with the
response for IDEMPOTENCY_TTL; abort() releases it when the request failed,
so the client may retry with the same key.
Keys are scoped per API key: one client can't replay another's upload.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Final

from redis import Redis

from app.api.errors import Conflict, FieldError, ValidationFailed

KEY_PREFIX: Final = "api:idem"
PENDING: Final = "__pending__"
PENDING_TTL_S: Final = 15 * 60  # longer than any upload request may take
_VALID_KEY: Final = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def check_key(key: str) -> str:
    if not _VALID_KEY.fullmatch(key):
        raise ValidationFailed(
            "Idempotency-Key must be 8-128 characters of [A-Za-z0-9._:-].",
            code="invalid_idempotency_key",
            errors=[FieldError(("header", "Idempotency-Key"), "invalid format")],
        )
    return key


class IdempotencyStore:
    def __init__(self, redis: Redis, *, ttl_s: int) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    @staticmethod
    def _key(scope: uuid.UUID, key: str) -> str:
        return f"{KEY_PREFIX}:{scope}:{key}"

    def begin(self, scope: uuid.UUID, key: str) -> Any | None:
        """None = first use (claimed). Otherwise the stored response."""
        name = self._key(scope, check_key(key))
        if self._redis.set(name, PENDING, nx=True, ex=PENDING_TTL_S):
            return None
        stored = self._redis.get(name)
        if stored is None:  # expired between SET and GET: claim it again
            return self.begin(scope, key)
        text = stored.decode() if isinstance(stored, bytes) else str(stored)
        if text == PENDING:
            raise Conflict(
                "A request with this Idempotency-Key is still in progress.", code="idempotency_in_progress"
            )
        return json.loads(text)

    def complete(self, scope: uuid.UUID, key: str, response: Any) -> None:
        self._redis.set(self._key(scope, key), json.dumps(response), ex=self._ttl_s)

    def abort(self, scope: uuid.UUID, key: str) -> None:
        self._redis.delete(self._key(scope, key))
