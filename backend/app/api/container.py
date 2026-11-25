"""Everything the API needs, built once per app (tests build their own)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from app.api.ratelimit import ApiRateLimiter
from app.config import ApiSettings, PipelineSettings, SearchSettings, ValidationSettings
from app.pipeline.storage import Storage
from app.search.types import SearchService
from app.validation.reconciliation import ReconciliationService
from app.validation.service import ValidationService
from app.workers.dispatch import JobEnqueuer


@dataclass(frozen=True, slots=True)
class Container:
    api: ApiSettings
    pipeline: PipelineSettings
    validation: ValidationSettings
    search_settings: SearchSettings
    session_factory: sessionmaker[Session]
    redis: Redis
    storage: Storage
    enqueuer: JobEnqueuer
    search: SearchService
    rate_limiter: ApiRateLimiter
    # /health/ready: component name -> check that raises when unavailable
    health_checks: Mapping[str, Callable[[], None]] = field(default_factory=dict)

    def validation_service(self) -> ValidationService:
        return ValidationService(session_factory=self.session_factory, settings=self.validation)

    def reconciliation_service(self) -> ReconciliationService:
        return ReconciliationService(session_factory=self.session_factory, settings=self.validation)


def build_container(api: ApiSettings) -> Container:
    """Production wiring from the environment (imports kept local: tests never pay for them)."""
    from app.api.services.ops import default_checks
    from app.config import (
        get_llm_settings,
        get_pipeline_settings,
        get_search_settings,
        get_validation_settings,
    )
    from app.db.session import get_sessionmaker
    from app.pipeline.storage import LocalStorage
    from app.search.runtime import build_runtime
    from app.search.service import HybridSearchService
    from app.workers.dispatch import CeleryEnqueuer

    pipeline = get_pipeline_settings()
    search = get_search_settings()
    session_factory = get_sessionmaker()
    redis = Redis.from_url(api.redis_url)
    runtime = build_runtime(get_llm_settings(), search, redis)
    return Container(
        api=api,
        pipeline=pipeline,
        validation=get_validation_settings(),
        search_settings=search,
        session_factory=session_factory,
        redis=redis,
        storage=LocalStorage(pipeline.storage_root),
        enqueuer=CeleryEnqueuer(),
        search=HybridSearchService(
            settings=search,
            min_similarity=api.search_min_similarity,
            query_embedder=runtime.query_embedder(redis, search.query_embed_cache_ttl_s),
        ),
        rate_limiter=ApiRateLimiter(redis, window_s=api.rate_window_s),
        health_checks=default_checks(session_factory, redis, pipeline.storage_root),
    )
