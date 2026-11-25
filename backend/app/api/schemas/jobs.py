"""Processing jobs and reprocessing requests."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas.common import ApiModel, UtcDateTime
from app.models.enums import ErrorCode, JobStatus, JobType
from app.workers.dispatch import FromStage

BULK_REPROCESS_HARD_MAX = 500


class JobOut(ApiModel):
    id: uuid.UUID
    document_id: uuid.UUID
    job_type: JobType
    status: JobStatus
    attempt: int
    triggered_by: str | None
    error_code: ErrorCode | None
    error_message: str | None
    queued_at: UtcDateTime
    started_at: UtcDateTime | None
    finished_at: UtcDateTime | None


class ReprocessRequest(ApiModel):
    from_stage: FromStage = Field(
        FromStage.PARSE,
        description="parse = everything; extract = LLM + validation; validate = validation only.",
    )
    force: bool = Field(False, description="extract: bypass the LLM response cache.")


class BulkReprocessRequest(ApiModel):
    error_codes: list[ErrorCode] | None = Field(None, max_length=len(ErrorCode))
    failed_since: datetime | None = None
    limit: int = Field(100, ge=1, le=BULK_REPROCESS_HARD_MAX)
    force: bool = Field(False, description="Also retry permanent errors (e.g. UNSUPPORTED_TYPE).")


class BulkReprocessItem(ApiModel):
    document_id: uuid.UUID
    outcome: Literal["queued", "skipped"]
    job_id: uuid.UUID | None = None
    reason: str | None = None


class BulkReprocessResponse(ApiModel):
    queued: int
    skipped: int
    items: list[BulkReprocessItem]
