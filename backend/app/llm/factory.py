"""Production wiring. Business code receives gateways; only this module builds them."""

from __future__ import annotations

from dataclasses import dataclass

from redis import Redis

from app.config import EMBEDDING_DIM, LLMSettings
from app.llm.budget import DailyQuota, SlidingWindowRateLimiter
from app.llm.cache import RedisResponseCache
from app.llm.catalog import CachedModelCatalog
from app.llm.chat import ChatGateway
from app.llm.circuit import CircuitBreaker
from app.llm.clock import Clock, SystemClock
from app.llm.embeddings import EmbeddingGateway
from app.llm.executor import RequestExecutor
from app.llm.transport import LLMTransport, OpenRouterMetadata, OpenRouterTransport


@dataclass(frozen=True, slots=True)
class Gateways:
    chat: ChatGateway
    embeddings: EmbeddingGateway
    quota: DailyQuota
    catalog: CachedModelCatalog


def build_gateways(
    settings: LLMSettings,
    redis: Redis,
    *,
    clock: Clock | None = None,
    transport: LLMTransport | None = None,
    metadata: OpenRouterMetadata | None = None,
) -> Gateways:
    clock = clock or SystemClock()
    metadata = metadata or OpenRouterMetadata(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout_s=settings.llm_connect_timeout_s * 3,
    )
    transport = transport or OpenRouterTransport(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        connect_timeout_s=settings.llm_connect_timeout_s,
        read_timeout_s=settings.llm_read_timeout_s,
    )
    quota = DailyQuota(
        redis,
        clock,
        default_limit=settings.free_model_daily_limit,
        source=metadata,
        resync_s=settings.quota_resync_s,
    )
    executor = RequestExecutor(
        limiter=SlidingWindowRateLimiter(
            redis, clock, limit=settings.free_model_rpm, window_s=settings.rate_window_s
        ),
        quota=quota,
        clock=clock,
        settings=settings,
    )
    breaker = CircuitBreaker(
        redis, clock, threshold=settings.circuit_failure_threshold, open_s=settings.circuit_open_s
    )
    catalog = CachedModelCatalog(metadata, redis, ttl_s=settings.model_catalog_ttl_s)
    chat = ChatGateway(
        transport=transport,
        catalog=catalog,
        executor=executor,
        breaker=breaker,
        cache=RedisResponseCache(redis, ttl_s=settings.llm_cache_ttl_s),
        clock=clock,
        settings=settings,
    )
    embeddings = EmbeddingGateway(
        transport=transport,
        catalog=catalog,
        executor=executor,
        breaker=breaker,
        redis=redis,
        settings=settings,
        dim=EMBEDDING_DIM,
    )
    return Gateways(chat=chat, embeddings=embeddings, quota=quota, catalog=catalog)
