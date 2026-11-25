"""Embedding work: the quota share, and draining the pending-chunk queue.

Embedding is OFF the critical path. A document is completed / needs_review as
soon as validation finishes; its chunks are keyword-searchable immediately and
gain vectors whenever quota allows. Running out of quota defers the work (the
task re-schedules itself after the UTC reset) and never fails a document.

The free tier is one account-wide budget shared with extraction, so
embeddings - document chunks AND search queries - may use at most
EMBED_QUOTA_SHARE of it (EmbedBudget). Each request is counted before it is
sent, like DailyQuota does for every request.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from redis import Redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.llm.budget import QuotaStatus, next_utc_midnight
from app.llm.errors import LLMError, QuotaExhaustedError
from app.models import Chunk
from app.search.types import EmbeddingClient

log = logging.getLogger(__name__)

KEY_PREFIX: Final = "search:embed"
DAY_KEY_TTL_S: Final = 2 * 24 * 3600


class QuotaReader(Protocol):
    """app.llm.budget.DailyQuota (only its status is read here)."""

    def status(self) -> QuotaStatus: ...


class EmbedBudget:
    """At most `share` of the day's free-tier limit goes to embeddings."""

    def __init__(
        self,
        redis: Redis,
        quota: QuotaReader,
        *,
        share: float,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._redis = redis
        self._quota = quota
        self._share = share
        self._now = now

    def _key(self) -> str:
        return f"{KEY_PREFIX}:day:{self._now().date().isoformat()}:used"

    def allowance(self) -> int:
        return math.floor(self._quota.status().limit * self._share)

    def used(self) -> int:
        return int(self._redis.get(self._key()) or 0)

    def try_take(self, requests: int = 1) -> bool:
        """Count `requests` about to be sent; False (and nothing counted) if over the share."""
        key = self._key()
        used = int(self._redis.incrby(key, requests))
        self._redis.expire(key, DAY_KEY_TTL_S)
        if used > self.allowance():
            self._redis.decrby(key, requests)
            return False
        return True

    def resets_at(self) -> datetime:
        return next_utc_midnight(self._now())


@dataclass(frozen=True, slots=True)
class EmbedReport:
    embedded: int = 0
    requests: int = 0
    deferred_until: datetime | None = None
    error: str | None = None  # transient failure (outage / open circuit): retry later
    spaces: tuple[str, ...] = ()

    @property
    def deferred(self) -> bool:
        return self.deferred_until is not None


@dataclass(frozen=True, slots=True)
class _Pending:
    id: uuid.UUID
    content: str
    content_sha256: str


class ChunkEmbedder:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        client: EmbeddingClient,
        budget: EmbedBudget,
        batch_size: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._budget = budget
        self._batch_size = batch_size
        self._now = now

    def embed_pending(self, *, document_id: uuid.UUID | None = None, limit: int) -> EmbedReport:
        """Embed up to `limit` pending chunks (one document, or the whole queue).

        One commit per batch, so progress survives a crash and a re-run picks up
        exactly where this one stopped (idempotent and resumable).
        """
        pending = self._pending(document_id, limit)
        return self._run(pending, spaces=None)

    def embed_chunks(self, chunk_ids: Sequence[uuid.UUID], *, spaces: Sequence[str]) -> EmbedReport:
        """Re-embed specific chunks into the given spaces (used by re-embedding)."""
        with self._session_factory() as session:
            rows = session.execute(
                select(Chunk.id, Chunk.content, Chunk.content_sha256).where(Chunk.id.in_(list(chunk_ids)))
            ).all()
        return self._run([_Pending(*r) for r in rows], spaces=spaces)

    def _run(self, pending: list[_Pending], *, spaces: Sequence[str] | None) -> EmbedReport:
        embedded = requests = 0
        seen: list[str] = []
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            if not self._budget.try_take():
                return EmbedReport(embedded, requests, self._budget.resets_at(), spaces=tuple(seen))
            requests += 1
            try:
                result = self._client.embed([p.content for p in batch], input_type="document", spaces=spaces)
            except QuotaExhaustedError as exc:
                return EmbedReport(embedded, requests, exc.resets_at, spaces=tuple(seen))
            except LLMError as exc:
                log.warning("embedding failed; chunks stay pending", extra={"error": type(exc).__name__})
                return EmbedReport(embedded, requests, error=type(exc).__name__, spaces=tuple(seen))
            embedded += self._store(batch, result.vectors, result.model_used, result.space)
            if result.space not in seen:
                seen.append(result.space)
        return EmbedReport(embedded, requests, spaces=tuple(seen))

    def _pending(self, document_id: uuid.UUID | None, limit: int) -> list[_Pending]:
        query = select(Chunk.id, Chunk.content, Chunk.content_sha256).where(Chunk.embedding.is_(None))
        if document_id is not None:
            query = query.where(Chunk.document_id == document_id)
        with self._session_factory() as session:
            rows = session.execute(query.order_by(Chunk.created_at, Chunk.chunk_index).limit(limit)).all()
        return [_Pending(*r) for r in rows]

    def _store(self, batch: list[_Pending], vectors: list[list[float]], model: str, space: str) -> int:
        """Guarded by content_sha256: a chunk re-written meanwhile keeps its new state."""
        now = self._now()
        stored = 0
        with self._session_factory() as session, session.begin():
            for item, vector in zip(batch, vectors, strict=True):
                result = session.execute(
                    update(Chunk)
                    .where(Chunk.id == item.id, Chunk.content_sha256 == item.content_sha256)
                    .values(embedding=vector, embedding_model=model, embedding_space=space, embedded_at=now)
                )
                stored += int(getattr(result, "rowcount", 0) or 0)
        return stored
