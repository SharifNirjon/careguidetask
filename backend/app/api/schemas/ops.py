"""Reconciliation runs, search results, stats and health."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import Field

from app.api.schemas.common import ApiModel, Money, UtcDateTime
from app.models.enums import ReviewStatus, RunStatus
from app.search.types import MatchType, SearchMode

MAX_RUN_DOCUMENTS = 500


class ReconciliationRequest(ApiModel):
    left_document_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_RUN_DOCUMENTS)
    right_document_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_RUN_DOCUMENTS)


class RunCreated(ApiModel):
    run_id: uuid.UUID
    status: RunStatus


class RunOut(ApiModel):
    id: uuid.UUID
    status: RunStatus
    left_document_ids: list[uuid.UUID]
    right_document_ids: list[uuid.UUID]
    created_by: str | None
    created_at: UtcDateTime
    finished_at: UtcDateTime | None
    report: dict[str, Any] = Field(
        description="matched, mismatched, only_in_left, only_in_right, ambiguous, "
        "counts. Empty while running."
    )


class SegmentOut(ApiModel):
    """Render as TEXT (never as HTML); emphasize when highlight is true."""

    text: str
    highlight: bool


class SearchHitOut(ApiModel):
    document_id: uuid.UUID
    extraction_id: uuid.UUID | None = Field(description="null when the hit can't be tied to one invoice.")
    score: float = Field(ge=0, le=1)
    match_type: MatchType
    unit_index: int | None = Field(description="Page / frame / sheet of the best match (for the preview).")
    snippet_segments: list[SegmentOut]
    filename: str
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    customer_name: str | None
    total: Money | None
    currency: str | None
    review_status: ReviewStatus | None


class SearchResponse(ApiModel):
    mode: SearchMode
    items: list[SearchHitOut]
    total: int = Field(description="Ranked results available (capped at MAX_SEARCH_RESULTS).")
    offset: int
    limit: int
    degraded: bool = Field(description="true: semantic ranking was unavailable; these are keyword results.")
    degraded_reason: str | None = None


class Percentiles(ApiModel):
    count: int
    p50_ms: int | None
    p95_ms: int | None


class Throughput(ApiModel):
    uploaded: int
    finished: int
    failed: int


class StatsOut(ApiModel):
    documents_by_status: dict[str, int]
    extractions_by_review_status: dict[str, int]
    open_issues_by_rule: dict[str, int]
    processing_time_by_file_type: dict[str, Percentiles]
    last_24h: Throughput


class ComponentHealth(ApiModel):
    ok: bool
    detail: str | None = None


class HealthOut(ApiModel):
    status: Literal["ok", "unavailable"]
    components: dict[str, ComponentHealth] = Field(default_factory=dict)
