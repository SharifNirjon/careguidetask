"""ORM rows -> response schemas. The only place that knows both shapes, so no
endpoint can leak an ORM object or an internal column by accident."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Final

from app.api.schemas.documents import DocumentSummary, EventOut, InvoiceHeadline, IssueCounts
from app.api.schemas.jobs import JobOut
from app.api.schemas.results import (
    CorrectionOut,
    ExtractionOut,
    ExtractionVersionOut,
    FieldOut,
    IssueOut,
    LineItemOut,
    ReconciliationOut,
)
from app.models import (
    Correction,
    Document,
    Extraction,
    LineItem,
    ProcessingEvent,
    ProcessingJob,
    Reconciliation,
    ValidationIssue,
)
from app.pipeline.types import FileKind

HEADER_FIELDS: Final = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "customer_name",
    "subtotal",
    "tax",
    "total",
    "currency",
)
LINE_FIELDS: Final = ("description", "quantity", "unit_price", "amount", "tax_rate")
REVIEW_META_KEY: Final = "_review"
DOCUMENT_TYPE_META_KEY: Final = "_document_type"
# Internal failure details (library messages, paths) stay in the DB and logs.
PRIVATE_EVENT_KEYS: Final = frozenset({"details", "error", "exception", "traceback"})


def as_text(value: Any) -> str | None:
    """Field value as a string: Decimals stay exact, dates are ISO."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def field_out(value: Any, meta: Mapping[str, Any] | None) -> FieldOut:
    meta = meta or {}
    confidence = meta.get("confidence")
    return FieldOut(
        value=as_text(value),
        raw_value=meta.get("raw_value"),
        confidence=float(confidence) if confidence is not None else None,
        grounded=meta.get("grounded"),
        evidence=meta.get("evidence_text"),
        unit_index=meta.get("unit_index"),
        source=meta.get("source"),
    )


def file_type(document: Document) -> str:
    try:
        return FileKind.from_mime(document.mime_type).value
    except StopIteration:
        return document.mime_type


def job_out(job: ProcessingJob) -> JobOut:
    return JobOut(
        id=job.id,
        document_id=job.document_id,
        job_type=job.job_type,
        status=job.status,
        attempt=job.attempt,
        triggered_by=job.triggered_by,
        error_code=job.error_code,
        error_message=job.error_message,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def headline(extraction: Extraction, counts: IssueCounts) -> InvoiceHeadline:
    return InvoiceHeadline(
        extraction_id=extraction.id,
        invoice_index=extraction.invoice_index,
        invoice_number=extraction.invoice_number,
        invoice_date=extraction.invoice_date,
        supplier_name=extraction.supplier_name,
        customer_name=extraction.customer_name,
        total=extraction.total,
        currency=as_text(extraction.currency),
        review_status=extraction.review_status,
        open_issues=counts,
    )


def document_summary(document: Document, invoices: list[InvoiceHeadline]) -> DocumentSummary:
    return DocumentSummary(
        id=document.id,
        filename=document.original_filename,
        file_type=file_type(document),
        size_bytes=document.size_bytes,
        status=document.status,
        error_code=document.error_code,
        uploaded_by=document.uploaded_by,
        duplicate_of_document_id=document.duplicate_of_document_id,
        created_at=document.created_at,
        invoices=invoices,
    )


def event_out(event: ProcessingEvent) -> EventOut:
    details = {k: v for k, v in (event.details or {}).items() if k not in PRIVATE_EVENT_KEYS}
    return EventOut(
        id=event.id,
        job_id=event.job_id,
        stage=event.stage,
        status=event.status,
        duration_ms=event.duration_ms,
        details=details,
        created_at=event.created_at,
    )


def issue_out(issue: ValidationIssue) -> IssueOut:
    return IssueOut(
        id=issue.id,
        extraction_id=issue.extraction_id,
        rule_code=issue.rule_code,
        severity=issue.severity,
        field_path=issue.field_path,
        message=issue.message,
        expected_value=issue.expected_value,
        actual_value=issue.actual_value,
        related_extraction_id=issue.related_extraction_id,
        resolved=issue.resolved_at is not None,
        resolved_at=issue.resolved_at,
        resolved_by=issue.resolved_by,
        resolution_note=issue.resolution_note,
        created_at=issue.created_at,
    )


def reconciliation_out(rec: Reconciliation, this_extraction: uuid.UUID) -> ReconciliationOut:
    """Differences are stored as left/right; flip them so "left" is always THIS extraction."""
    is_left = rec.left_extraction_id == this_extraction
    differences = rec.differences or []
    if not is_left:
        differences = [{**d, "left": d.get("right"), "right": d.get("left")} for d in differences]
    return ReconciliationOut(
        id=rec.id,
        counterpart_extraction_id=rec.right_extraction_id if is_left else rec.left_extraction_id,
        match_method=rec.match_method,
        status=rec.status,
        differences=differences,
        run_id=rec.run_id,
        created_at=rec.created_at,
    )


def line_item_out(item: LineItem) -> LineItemOut:
    meta = item.field_meta or {}
    return LineItemOut(
        id=item.id,
        position=item.position,
        description=item.description,
        quantity=item.quantity,
        unit_price=item.unit_price,
        amount=item.amount,
        tax_rate=item.tax_rate,
        fields={name: field_out(getattr(item, name), meta.get(name)) for name in LINE_FIELDS},
    )


def extraction_out(
    extraction: Extraction, issues: Iterable[ValidationIssue], reconciliations: Iterable[Reconciliation]
) -> ExtractionOut:
    meta = extraction.field_meta or {}
    review = meta.get(REVIEW_META_KEY) or {}
    return ExtractionOut(
        id=extraction.id,
        document_id=extraction.document_id,
        invoice_index=extraction.invoice_index,
        version=extraction.version,
        is_current=extraction.is_current,
        lock_version=extraction.lock_version,
        document_type=str(meta.get(DOCUMENT_TYPE_META_KEY) or "invoice"),
        review_status=extraction.review_status,
        reviewed_by=extraction.reviewed_by,
        reviewed_at=extraction.reviewed_at,
        review_note=review.get("note"),
        overall_confidence=float(extraction.overall_confidence)
        if extraction.overall_confidence is not None
        else None,
        model_name=extraction.model_name,
        prompt_version=extraction.prompt_version,
        fields={name: field_out(getattr(extraction, name), meta.get(name)) for name in HEADER_FIELDS},
        line_items=[line_item_out(i) for i in sorted(extraction.line_items, key=lambda li: li.position)],
        issues=[issue_out(i) for i in issues if i.extraction_id == extraction.id],
        reconciliations=[
            reconciliation_out(r, extraction.id)
            for r in reconciliations
            if extraction.id in (r.left_extraction_id, r.right_extraction_id)
        ],
        created_at=extraction.created_at,
        updated_at=extraction.updated_at,
    )


def version_out(extraction: Extraction) -> ExtractionVersionOut:
    return ExtractionVersionOut(
        id=extraction.id,
        version=extraction.version,
        is_current=extraction.is_current,
        review_status=extraction.review_status,
        overall_confidence=float(extraction.overall_confidence)
        if extraction.overall_confidence is not None
        else None,
        model_name=extraction.model_name,
        invoice_number=extraction.invoice_number,
        total=extraction.total,
        created_at=extraction.created_at,
    )


def correction_out(correction: Correction) -> CorrectionOut:
    return CorrectionOut(
        id=correction.id,
        extraction_id=correction.extraction_id,
        line_item_id=correction.line_item_id,
        field_path=correction.field_path,
        old_value=correction.old_value,
        new_value=correction.new_value,
        corrected_by=correction.corrected_by,
        reason=correction.reason,
        created_at=correction.created_at,
    )
