"""SQL for api_keys."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import ApiKey
from app.models.enums import ApiRole


class ApiKeyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def by_prefix(self, prefix: str) -> ApiKey | None:
        return self._session.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))

    def create(self, *, name: str, prefix: str, key_hash: str, role: ApiRole) -> ApiKey:
        key = ApiKey(name=name, key_prefix=prefix, key_hash=key_hash, role=role)
        self._session.add(key)
        self._session.flush()
        return key

    def revoke(self, prefix: str, now: datetime) -> bool:
        result = self._session.execute(
            update(ApiKey)
            .where(ApiKey.key_prefix == prefix, ApiKey.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return bool(getattr(result, "rowcount", 0))

    def touch(self, key_id: uuid.UUID, now: datetime, *, not_since: datetime) -> None:
        """Write last_used_at at most once per interval: a hot key must not turn
        every read request into a row update."""
        self._session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, (ApiKey.last_used_at.is_(None)) | (ApiKey.last_used_at < not_since))
            .values(last_used_at=now)
        )
