"""Semantic retrieval: per-space k-NN over chunk embeddings.

Vectors of different spaces are never compared: the query is embedded once
per space that holds matching chunks (normally one), and each space is
searched separately with `embedding_space = '<space>'` rendered as a literal,
so the planner can match it to that space's PARTIAL HNSW index.

Filtered vector search: an HNSW scan returns the ef_search nearest candidates
and filters apply afterwards, so a selective filter could silently return
fewer than k rows. hnsw.ef_search is raised per query (SEARCH_EF), and on
pgvector >= 0.8 hnsw.iterative_scan = relaxed_order keeps scanning the index
until k rows pass the filters (results may come slightly out of order, so
they are re-sorted by distance here).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from redis import Redis
from sqlalchemy import literal, select, text
from sqlalchemy.orm import Session

from app.config import EMBEDDING_DIM, EMBEDDING_SPACES
from app.llm.errors import QuotaExhaustedError
from app.models import Chunk
from app.search.embedding import EmbedBudget
from app.search.filters import DocumentFilters
from app.search.keyword import ChunkHit, chunk_base
from app.search.types import EmbeddingClient

CACHE_PREFIX: Final = "search:qvec"
MAX_COSINE_DISTANCE: Final = 2.0
ITERATIVE_SCAN: Final = "relaxed_order"
PROBE_VECTOR: Final = [1.0] + [0.0] * (EMBEDDING_DIM - 1)  # any constant: only existence matters


class QueryEmbeddingUnavailable(Exception):
    """The query could not be embedded (quota share, provider quota, outage)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_query(text_: str) -> str:
    return " ".join(text_.casefold().split())


def similarity(distance: float) -> float:
    """Cosine distance in [0, 2] -> similarity in [0, 1] (1 = same direction)."""
    return max(0.0, min(1.0, 1.0 - distance / MAX_COSINE_DISTANCE))


class QueryEmbedder:
    """Embeds a search query in one space, cached in Redis per (query, space).

    A cache hit spends no request. A miss spends one, counted against the
    embedding share of the free-tier budget like any other embedding.
    """

    def __init__(self, *, client: EmbeddingClient, redis: Redis, budget: EmbedBudget, ttl_s: int) -> None:
        self._client = client
        self._redis = redis
        self._budget = budget
        self._ttl_s = ttl_s

    @staticmethod
    def cache_key(query: str, space: str) -> str:
        digest = hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()
        return f"{CACHE_PREFIX}:{space}:{digest}"  # hashed: the query text never sits in Redis keys

    def embed(self, query: str, space: str) -> list[float]:
        key = self.cache_key(query, space)
        cached = self._redis.get(key)
        if cached is not None:
            vector: list[float] = json.loads(cached)
            return vector
        if not self._budget.try_take():
            raise QueryEmbeddingUnavailable("embedding quota share used up for today")
        try:
            result = self._client.embed([normalize_query(query)], input_type="query", spaces=[space])
        except QuotaExhaustedError as exc:
            raise QueryEmbeddingUnavailable("free-tier quota exhausted") from exc
        except Exception as exc:  # outage, open circuit, auth: degrade to keyword, never a 500
            raise QueryEmbeddingUnavailable(f"embedding unavailable ({type(exc).__name__})") from exc
        if result.space != space or not result.vectors:
            raise QueryEmbeddingUnavailable(f"no model available for space {space}")
        self._redis.set(key, json.dumps(result.vectors[0]), ex=self._ttl_s)
        return result.vectors[0]


@dataclass(frozen=True, slots=True)
class SpaceHits:
    space: str
    hits: list[ChunkHit]


class SemanticRetriever:
    def __init__(self, *, ef_search: int, iterative_scan: bool = True) -> None:
        self._ef_search = ef_search
        self._iterative_scan = iterative_scan

    @staticmethod
    def spaces_with_embeddings(session: Session) -> list[str]:
        """Spaces holding at least one embedded chunk (each is worth a query embedding).

        Probed through each space's own HNSW index (`ORDER BY embedding <=> const
        LIMIT 1`): instant, even for an empty space. An equality probe on
        embedding_space would scan the table for every space without rows, on
        every query. Filters are not applied here; the vector search applies them.
        """
        probe = Chunk.embedding.cosine_distance(PROBE_VECTOR)
        # When every row is in one space the planner prefers seq scan + top-N sort for
        # LIMIT 1 (a full pass); the index answers in ~1 ms. Restored right after, so the
        # real searches in this transaction plan normally.
        # One round trip: a scalar subquery per space (each keeps its ORDER BY ... LIMIT 1).
        probes = [
            select(Chunk.id)
            .where(Chunk.embedding_space == literal(space, literal_execute=True))
            .order_by(probe)
            .limit(1)
            .scalar_subquery()
            for space in EMBEDDING_SPACES
        ]
        session.execute(text("SELECT set_config('enable_seqscan', 'off', true)"))
        try:
            row = session.execute(select(*probes)).one()
        finally:
            session.execute(text("SELECT set_config('enable_seqscan', 'on', true)"))
        return [space for space, hit in zip(EMBEDDING_SPACES, row, strict=True) if hit is not None]

    def search(
        self, session: Session, vector: Sequence[float], space: str, filters: DocumentFilters, limit: int
    ) -> SpaceHits:
        if space not in EMBEDDING_SPACES:
            raise ValueError(f"unknown embedding space {space!r}")
        session.execute(text("SELECT set_config('hnsw.ef_search', :ef, true)"), {"ef": str(self._ef_search)})
        if self._iterative_scan:
            session.execute(
                text("SELECT set_config('hnsw.iterative_scan', :mode, true)"), {"mode": ITERATIVE_SCAN}
            )
        distance = Chunk.embedding.cosine_distance(list(vector))
        rows = session.execute(
            chunk_base(distance, filters)
            .where(Chunk.embedding_space == literal(space, literal_execute=True))
            .order_by(distance)
            .limit(limit)
        ).all()
        hits = sorted(
            (ChunkHit(r[0], r[1], r[2], r[3], similarity(float(r[4]))) for r in rows),
            key=lambda h: (-h.score, h.chunk_id),
        )
        return SpaceHits(space, hits)
