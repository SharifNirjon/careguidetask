"""/jobs, /reconciliations, /search, /stats and /health."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import AdminRead, AdminWrite, ContainerDep, ReviewerWrite, SessionDep, Viewer
from app.api.errors import FieldError, ValidationFailed
from app.api.routers.params import PAGE_DEFAULT, Limit, document_filters
from app.api.schemas.common import problems
from app.api.schemas.jobs import BulkReprocessRequest, BulkReprocessResponse, JobOut
from app.api.schemas.ops import HealthOut, ReconciliationRequest, RunCreated, RunOut, SearchResponse, StatsOut
from app.api.services import ops
from app.api.services.filters import DocumentFilters
from app.api.services.jobs import JobService
from app.api.services.search import SearchMode, SearchQuery

jobs = APIRouter(prefix="/jobs", tags=["jobs"])
reconciliations = APIRouter(prefix="/reconciliations", tags=["reconciliations"])
search = APIRouter(tags=["search"])
health = APIRouter(prefix="/health", tags=["health"])

SEARCH_HARD_MAX_CHARS = 500


@jobs.post(
    "/reprocess-failed",
    response_model=BulkReprocessResponse,
    summary="Bulk reprocess failed documents",
    description="Admin. Permanent errors (e.g. UNSUPPORTED_TYPE) are skipped unless force=true.",
    responses=problems(403),
)
def reprocess_failed(
    body: BulkReprocessRequest, principal: AdminWrite, session: SessionDep, container: ContainerDep
) -> BulkReprocessResponse:
    return JobService(session, container).reprocess_failed(body, principal)


@jobs.get("/{job_id}", response_model=JobOut, summary="Get a job", responses=problems(403, 404))
def get_job(
    job_id: Annotated[uuid.UUID, Path()], _: Viewer, session: SessionDep, container: ContainerDep
) -> JobOut:
    return JobService(session, container).get(job_id)


@reconciliations.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunCreated,
    summary="Start a batch reconciliation",
    description="Matches the current invoices of two document sets (e.g. PDFs vs an Excel "
    "ledger). Poll GET /reconciliations/{run_id} for the report.",
    responses=problems(403),
)
def create_reconciliation(
    body: ReconciliationRequest, principal: ReviewerWrite, session: SessionDep, container: ContainerDep
) -> RunCreated:
    return ops.ReconciliationApiService(session, container).create(body, principal)


@reconciliations.get(
    "/{run_id}", response_model=RunOut, summary="Reconciliation status + report", responses=problems(403, 404)
)
def get_reconciliation(
    run_id: Annotated[uuid.UUID, Path()], _: Viewer, session: SessionDep, container: ContainerDep
) -> RunOut:
    return ops.ReconciliationApiService(session, container).get(run_id)


@search.get(
    "/search",
    response_model=SearchResponse,
    summary="Search invoices",
    description="keyword: trigram/ILIKE over invoice number, supplier and customer, plus the same filters "
    "as GET /documents. semantic / hybrid return 501 until Step 7.",
    responses=problems(403, 501),
)
def search_invoices(
    _: Viewer,
    session: SessionDep,
    container: ContainerDep,
    filters: Annotated[DocumentFilters, Depends(document_filters)],
    q: Annotated[str, Query(min_length=1, max_length=SEARCH_HARD_MAX_CHARS)],
    mode: SearchMode = SearchMode.KEYWORD,
    limit: Limit = PAGE_DEFAULT,
) -> SearchResponse:
    if len(q) > container.api.search_query_max_chars:
        raise ValidationFailed(
            "The query is too long.",
            code="query_too_long",
            errors=[FieldError(("query", "q"), "too long", "string_too_long")],
        )
    return ops.search(session, container, SearchQuery(text=q, mode=mode, filters=filters, limit=limit))


@search.get(
    "/stats",
    response_model=StatsOut,
    summary="Operational statistics (admin)",
    tags=["ops"],
    responses=problems(403),
)
def get_stats(_: AdminRead, session: SessionDep) -> StatsOut:
    return ops.stats(session)


@health.get("/live", response_model=HealthOut, summary="Liveness", description="The process is up. No auth.")
def live() -> HealthOut:
    return HealthOut(status="ok")


@health.get(
    "/ready",
    response_model=HealthOut,
    summary="Readiness",
    description="DB, Redis and storage reachable; otherwise 503 with per-component status. No auth.",
    responses={503: {"model": HealthOut, "description": "A dependency is unavailable"}},
)
def ready(container: ContainerDep) -> JSONResponse:
    result = ops.readiness(container.health_checks)
    code = status.HTTP_200_OK if result.status == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(result.model_dump(mode="json"), status_code=code)
