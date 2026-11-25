"""/jobs, /reconciliations, /search, /stats and /health."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import AdminRead, AdminWrite, ContainerDep, ReviewerWrite, Searcher, SessionDep, Viewer
from app.api.errors import FieldError, ValidationFailed
from app.api.routers.params import PAGE_DEFAULT, Limit, document_filters
from app.api.schemas.common import problems
from app.api.schemas.jobs import BulkReprocessRequest, BulkReprocessResponse, JobOut
from app.api.schemas.ops import HealthOut, ReconciliationRequest, RunCreated, RunOut, SearchResponse, StatsOut
from app.api.services import ops
from app.api.services.jobs import JobService
from app.search.filters import DocumentFilters
from app.search.types import SearchMode, SearchQuery

jobs = APIRouter(prefix="/jobs", tags=["jobs"])
reconciliations = APIRouter(prefix="/reconciliations", tags=["reconciliations"])
search = APIRouter(tags=["search"])
health = APIRouter(prefix="/health", tags=["health"])

SEARCH_HARD_MAX_CHARS = 500
SEARCH_HARD_MAX_OFFSET = 10_000  # schema bound; the real cap is MAX_SEARCH_RESULTS


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
    description=(
        'keyword: full text over document chunks ("phrases", OR, -exclusion) plus invoice number, '
        "supplier and customer matches. semantic: vector similarity. hybrid (default): both, fused with "
        "Reciprocal Rank Fusion. Same filters as GET /documents. If the query can't be embedded (quota, "
        "outage), semantic and hybrid return keyword results with degraded=true. Snippets are structured "
        "text segments, never HTML. Stricter rate limit (SEARCH_RPM): semantic queries spend embedding quota."
    ),
    responses=problems(403),
)
def search_invoices(
    _: Searcher,
    session: SessionDep,
    container: ContainerDep,
    filters: Annotated[DocumentFilters, Depends(document_filters)],
    q: Annotated[str, Query(min_length=1, max_length=SEARCH_HARD_MAX_CHARS)],
    mode: SearchMode = SearchMode.HYBRID,
    limit: Limit = PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0, le=SEARCH_HARD_MAX_OFFSET, description="Ranked results to skip.")] = 0,
) -> SearchResponse:
    if len(q) > container.api.search_query_max_chars:
        raise ValidationFailed(
            "The query is too long.",
            code="query_too_long",
            errors=[FieldError(("query", "q"), "too long", "string_too_long")],
        )
    cap = container.search_settings.max_search_results
    if offset + limit > cap:
        raise ValidationFailed(
            f"Search results are capped at {cap}: offset + limit must not exceed it.",
            code="offset_out_of_range",
            errors=[FieldError(("query", "offset"), f"offset + limit > {cap}", "less_than_equal")],
        )
    query = SearchQuery(text=q, mode=mode, filters=filters, limit=limit, offset=offset)
    return ops.search(session, container, query)


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
