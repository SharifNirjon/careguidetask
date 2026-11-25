"""Reconciliation runs, search, stats and health."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from redis import Redis
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import Principal
from app.api.container import Container
from app.api.errors import FieldError, NotFound, ValidationFailed
from app.api.repositories.stats import StatsRepository
from app.api.schemas.ops import (
    ComponentHealth,
    HealthOut,
    Percentiles,
    ReconciliationRequest,
    RunCreated,
    RunOut,
    SearchHitOut,
    SearchResponse,
    StatsOut,
    Throughput,
)
from app.api.services.search import SearchQuery
from app.models import Document, ReconciliationRun
from app.models.enums import RunStatus
from app.pipeline.types import FileKind

log = logging.getLogger(__name__)

THROUGHPUT_WINDOW: Final = timedelta(hours=24)


class ReconciliationApiService:
    def __init__(self, session: Session, container: Container) -> None:
        self._session = session
        self._c = container

    def create(self, request: ReconciliationRequest, principal: Principal) -> RunCreated:
        wanted = {*request.left_document_ids, *request.right_document_ids}
        found = set(
            self._session.scalars(
                select(Document.id).where(Document.id.in_(wanted), Document.deleted_at.is_(None))
            )
        )
        missing = sorted(str(i) for i in wanted - found)
        if missing:
            raise ValidationFailed(
                "Some documents do not exist.",
                code="unknown_documents",
                errors=[
                    FieldError(("body", "document_ids"), f"unknown document {m}", "not_found")
                    for m in missing
                ],
            )
        run_id = self._c.reconciliation_service().create_run(
            request.left_document_ids, request.right_document_ids, principal.name
        )
        try:
            self._c.enqueuer.enqueue_reconciliation(run_id)
        except Exception:
            log.warning("reconciliation enqueue failed", extra={"run_id": str(run_id)}, exc_info=True)
        return RunCreated(run_id=run_id, status=RunStatus.RUNNING)

    def get(self, run_id: uuid.UUID) -> RunOut:
        run = self._session.get(ReconciliationRun, run_id)
        if run is None:
            raise NotFound(f"Reconciliation run {run_id} was not found.", code="run_not_found")
        return RunOut(
            id=run.id,
            status=run.status,
            left_document_ids=run.left_document_ids,
            right_document_ids=run.right_document_ids,
            created_by=run.created_by,
            created_at=run.created_at,
            finished_at=run.finished_at,
            report=run.summary or {},
        )


def search(session: Session, container: Container, query: SearchQuery) -> SearchResponse:
    hits = container.search.search(session, query)
    return SearchResponse(
        mode=query.mode,
        items=[
            SearchHitOut(
                document_id=h.document_id,
                extraction_id=h.extraction_id,
                score=h.score,
                snippet=h.snippet,
                invoice_number=h.invoice_number,
                invoice_date=h.invoice_date,
                supplier_name=h.supplier_name,
                customer_name=h.customer_name,
                total=h.total,
                currency=h.currency,
                review_status=h.review_status,
            )
            for h in hits
        ],
    )


def stats(session: Session, now: datetime | None = None) -> StatsOut:
    repo = StatsRepository(session)
    by_type: dict[str, Percentiles] = {}
    for mime, (count, p50, p95) in repo.processing_ms_by_mime().items():
        try:
            name = FileKind.from_mime(mime).value
        except StopIteration:
            name = mime
        by_type[name] = Percentiles(
            count=count,
            p50_ms=None if p50 is None else round(p50),
            p95_ms=None if p95 is None else round(p95),
        )
    uploaded, finished, failed = repo.throughput_since((now or datetime.now(UTC)) - THROUGHPUT_WINDOW)
    return StatsOut(
        documents_by_status=repo.documents_by_status(),
        extractions_by_review_status=repo.extractions_by_review_status(),
        open_issues_by_rule=repo.open_issues_by_rule(),
        processing_time_by_file_type=by_type,
        last_24h=Throughput(uploaded=uploaded, finished=finished, failed=failed),
    )


HealthCheck = Callable[[], None]


def default_checks(
    session_factory: sessionmaker[Session], redis: Redis, storage_root: Path
) -> dict[str, HealthCheck]:
    def database() -> None:
        with session_factory() as session:
            session.execute(text("SELECT 1"))

    def cache() -> None:
        redis.ping()

    def storage() -> None:
        if not storage_root.is_dir():
            raise OSError("storage root is missing")

    return {"database": database, "redis": cache, "storage": storage}


def readiness(checks: Mapping[str, HealthCheck]) -> HealthOut:
    """Details name the failing component and the exception TYPE only: no hosts,
    no credentials, nothing an unauthenticated caller shouldn't see."""
    components: dict[str, ComponentHealth] = {}
    for name, check in checks.items():
        try:
            check()
            components[name] = ComponentHealth(ok=True)
        except Exception as exc:
            log.warning("readiness check failed", extra={"component": name, "error": type(exc).__name__})
            components[name] = ComponentHealth(ok=False, detail=type(exc).__name__)
    ok = all(c.ok for c in components.values())
    return HealthOut(status="ok" if ok else "unavailable", components=components)
