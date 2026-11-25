"""Move chunks embedded in a fallback space into the primary space.

When the primary embedding model was down or rate-limited, chunks were embedded
by a fallback model (another space). They are searchable, but only against
queries in that space. Re-embedding moves them back once the primary model is
healthy (circuit closed) and the embedding share of the quota allows.

Idempotent and resumable: each batch commits on its own and only selects rows
still in the source space, so a re-run continues where the last one stopped.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Chunk
from app.search.embedding import ChunkEmbedder, EmbedReport


@dataclass(frozen=True, slots=True)
class ReembedReport:
    selected: int
    moved: int
    skipped_reason: str | None = None
    embed: EmbedReport | None = None


class Reembedder:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        embedder: ChunkEmbedder,
        primary_space: str,
        primary_healthy: Callable[[], bool],
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._primary = primary_space
        self._healthy = primary_healthy

    def run(self, *, from_spaces: Sequence[str], limit: int) -> ReembedReport:
        sources = [s for s in from_spaces if s != self._primary]
        if not sources:
            return ReembedReport(0, 0, "nothing to move: the source is the primary space")
        if not self._healthy():
            return ReembedReport(0, 0, "primary embedding model unavailable (circuit open)")
        with self._session_factory() as session:
            ids = list(
                session.scalars(
                    select(Chunk.id)
                    .where(Chunk.embedding_space.in_(sources))
                    .order_by(Chunk.embedded_at, Chunk.id)
                    .limit(limit)
                )
            )
        if not ids:
            return ReembedReport(0, 0)
        report = self._embedder.embed_chunks(ids, spaces=[self._primary])
        reason = None
        if report.deferred:
            reason = "embedding quota share used up"
        elif report.error:
            reason = f"primary model failed ({report.error})"
        return ReembedReport(len(ids), report.embedded, reason, report)
