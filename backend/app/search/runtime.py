"""Production wiring for embedding work (only workers, the CLI and the API call this)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from app.config import LLMSettings, SearchSettings
from app.llm.circuit import CircuitBreaker
from app.llm.clock import SystemClock
from app.llm.factory import build_gateways
from app.search.embedding import ChunkEmbedder, EmbedBudget
from app.search.reembed import Reembedder
from app.search.semantic import QueryEmbedder
from app.search.types import EmbeddingClient


@dataclass(frozen=True, slots=True)
class SearchRuntime:
    client: EmbeddingClient
    budget: EmbedBudget
    primary_space: str
    primary_healthy: Callable[[], bool]

    def chunk_embedder(self, session_factory: sessionmaker[Session], batch_size: int) -> ChunkEmbedder:
        return ChunkEmbedder(
            session_factory=session_factory, client=self.client, budget=self.budget, batch_size=batch_size
        )

    def reembedder(self, session_factory: sessionmaker[Session], batch_size: int) -> Reembedder:
        return Reembedder(
            session_factory=session_factory,
            embedder=self.chunk_embedder(session_factory, batch_size),
            primary_space=self.primary_space,
            primary_healthy=self.primary_healthy,
        )

    def query_embedder(self, redis: Redis, ttl_s: int) -> QueryEmbedder:
        return QueryEmbedder(client=self.client, redis=redis, budget=self.budget, ttl_s=ttl_s)


def build_runtime(llm: LLMSettings, search: SearchSettings, redis: Redis) -> SearchRuntime:
    gateways = build_gateways(llm, redis)
    # Same Redis keys as the gateway's own breaker: this only READS the shared state.
    breaker = CircuitBreaker(
        redis, SystemClock(), threshold=llm.circuit_failure_threshold, open_s=llm.circuit_open_s
    )
    primary = llm.embedding_models[0]
    return SearchRuntime(
        client=gateways.embeddings,
        budget=EmbedBudget(redis, gateways.quota, share=search.embed_quota_share),
        primary_space=primary.space,
        primary_healthy=lambda: not breaker.is_open(primary.model),
    )
