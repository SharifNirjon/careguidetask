"""Authenticate bearer keys; create/revoke keys (CLI)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session

from app.api.auth import GeneratedKey, Principal, generate_key, prefix_of, verify
from app.api.errors import Unauthorized
from app.api.repositories.api_keys import ApiKeyRepository
from app.models.enums import ApiRole

log = logging.getLogger(__name__)

TOUCH_INTERVAL: Final = timedelta(seconds=60)
BEARER_CHALLENGE: Final = {"WWW-Authenticate": "Bearer"}


def _unauthorized(detail: str = "A valid API key is required.") -> Unauthorized:
    return Unauthorized(detail, code="unauthorized", headers=dict(BEARER_CHALLENGE))


class AuthService:
    def __init__(self, session: Session, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._session = session
        self._repo = ApiKeyRepository(session)
        self._now = now

    def authenticate(self, token: str | None) -> Principal:
        """Same answer for unknown, malformed and revoked keys: nothing to probe."""
        prefix = prefix_of(token) if token else None
        key = self._repo.by_prefix(prefix) if prefix else None
        if token is None or key is None or not verify(token, key.key_hash) or not key.is_active:
            raise _unauthorized()
        now = self._now()
        self._repo.touch(key.id, now, not_since=now - TOUCH_INTERVAL)
        self._session.commit()
        return Principal(key_id=key.id, name=key.name, role=key.role)

    def create_key(self, name: str, role: ApiRole) -> GeneratedKey:
        generated = generate_key()
        self._repo.create(name=name, prefix=generated.prefix, key_hash=generated.key_hash, role=role)
        self._session.commit()
        log.info("api key created", extra={"key_prefix": generated.prefix, "role": role.value})
        return generated

    def revoke_key(self, key_or_prefix: str) -> bool:
        prefix = prefix_of(key_or_prefix) or key_or_prefix
        revoked = self._repo.revoke(prefix, self._now())
        self._session.commit()
        if revoked:
            log.info("api key revoked", extra={"key_prefix": prefix})
        return revoked
