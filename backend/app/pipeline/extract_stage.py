"""The `extract` stage: run the extractor, translate LLM outcomes into pipeline terms.

  QuotaExhaustedError   -> DeferredError (resume after the UTC reset; NOT a failure)
  LLMUnavailableError   -> TransientError(LLM_UNAVAILABLE)   (Celery retries later)
  LLMAuthError          -> PermanentError(LLM_ERROR)         (configuration problem)

Per-segment reports become processing_events (duration, model, tokens,
requests, cache hits) for the evaluation report.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.extraction.assemble import NOT_AN_INVOICE_NOTE
from app.extraction.service import ExtractionReport, SegmentReport
from app.llm import LLMAuthError, LLMUnavailableError, QuotaExhaustedError
from app.models import EventStatus
from app.models.enums import ErrorCode
from app.pipeline.errors import DeferredError, PermanentError, TransientError

SEGMENT_STATUS = {
    "succeeded": EventStatus.SUCCEEDED,
    "failed": EventStatus.FAILED,
    "already_done": EventStatus.SKIPPED,
    "auxiliary_sheet": EventStatus.SKIPPED,
}


class DocumentExtractor(Protocol):
    def extract_document(
        self, document_id: uuid.UUID, job_id: uuid.UUID, *, force: bool = False
    ) -> ExtractionReport: ...


@dataclass(frozen=True, slots=True)
class EventSpec:
    status: EventStatus
    duration_ms: int | None
    details: dict[str, Any] = field(default_factory=dict)


def run_extract(
    extractor: DocumentExtractor, document_id: uuid.UUID, job_id: uuid.UUID, *, force: bool
) -> ExtractionReport:
    """Raises DeferredError / TransientError / PermanentError; nothing LLM-specific escapes."""
    try:
        return extractor.extract_document(document_id, job_id, force=force)
    except QuotaExhaustedError as exc:
        raise DeferredError(exc.resets_at) from exc
    except LLMUnavailableError as exc:
        raise TransientError(ErrorCode.LLM_UNAVAILABLE, details={"failures": exc.failures}) from exc
    except LLMAuthError as exc:
        raise PermanentError(ErrorCode.LLM_ERROR, details={"reason": "auth"}) from exc


def segment_event(report: SegmentReport) -> EventSpec:
    details = {"scope": "segment", **report.to_details()}
    if report.document_type == "other":
        details["note"] = NOT_AN_INVOICE_NOTE
    return EventSpec(SEGMENT_STATUS[report.status], report.duration_ms, details)


def summary_event(report: ExtractionReport, duration_ms: int) -> EventSpec:
    segments = report.segments
    return EventSpec(
        EventStatus.SUCCEEDED,
        duration_ms,
        {
            "scope": "document",
            "segments": len(segments),
            "failed_segments": sum(s.status == "failed" for s in segments),
            "rule_segments": sum(s.source == "rule" for s in segments),
            "llm_requests": report.llm_requests,
            "cached_parts": sum(s.cached_parts for s in segments),
            "not_an_invoice": sum(s.document_type == "other" for s in segments),
        },
    )
