"""Documents: upload outcomes, list summaries, detail + progress, events."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import Field

from app.api.schemas.common import ApiModel, Money, UtcDateTime
from app.api.schemas.jobs import JobOut
from app.models.enums import DocumentStatus, ErrorCode, EventStatus, ReviewStatus, Stage


class UploadError(ApiModel):
    code: ErrorCode
    detail: str


class UploadOutcome(ApiModel):
    filename: str
    document_id: uuid.UUID | None = None
    status: Literal["queued", "rejected"]
    duplicate_of_document_id: uuid.UUID | None = None
    error: UploadError | None = None


class IssueCounts(ApiModel):
    error: int = 0
    warning: int = 0
    info: int = 0


class InvoiceHeadline(ApiModel):
    extraction_id: uuid.UUID
    invoice_index: int
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    customer_name: str | None
    total: Money | None
    currency: str | None
    review_status: ReviewStatus
    open_issues: IssueCounts


class DocumentSummary(ApiModel):
    id: uuid.UUID
    filename: str
    file_type: str = Field(examples=["pdf"])
    size_bytes: int
    status: DocumentStatus
    error_code: ErrorCode | None
    uploaded_by: str | None
    duplicate_of_document_id: uuid.UUID | None
    created_at: UtcDateTime
    invoices: list[InvoiceHeadline] = Field(description="Every current invoice found in the document.")


class DocumentError(ApiModel):
    code: ErrorCode
    message: str


class Progress(ApiModel):
    stage: Stage | None = Field(description="Stage in progress; null when the job finished.")
    stages_done: int
    stages_total: int
    units_done: int
    units_total: int | None
    percent: int = Field(ge=0, le=100)
    started_at: UtcDateTime | None
    elapsed_ms: int | None


class DocumentDetail(ApiModel):
    id: uuid.UUID
    filename: str
    file_type: str
    mime_type: str
    size_bytes: int
    sha256: str
    status: DocumentStatus
    error: DocumentError | None
    page_count: int | None
    unit_count: int
    uploaded_by: str | None
    duplicate_of_document_id: uuid.UUID | None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    processing_started_at: UtcDateTime | None
    processing_finished_at: UtcDateTime | None
    latest_job: JobOut | None
    progress: Progress

    def etag_basis(self) -> DocumentDetail:
        """Everything but elapsed_ms (derivable from progress.started_at)."""
        return self.model_copy(update={"progress": self.progress.model_copy(update={"elapsed_ms": None})})


class EventOut(ApiModel):
    id: int
    job_id: uuid.UUID | None
    stage: Stage
    status: EventStatus
    duration_ms: int | None
    details: dict[str, Any]
    created_at: UtcDateTime
