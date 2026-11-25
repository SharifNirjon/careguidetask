"""Extraction results, corrections and review requests."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field

from app.api.schemas.common import ApiModel, Money, UtcDateTime
from app.models.enums import (
    DocumentStatus,
    MatchMethod,
    ReconciliationStatus,
    ReviewStatus,
    RuleCode,
    Severity,
)

MAX_VALUE_CHARS = 500
MAX_NOTE_CHARS = 2000
MAX_LINE_OPS = 500


class FieldOut(ApiModel):
    value: str | None = Field(description="Normalized value as a string (amounts are decimal strings).")
    raw_value: str | None = None
    confidence: float | None = None
    grounded: bool | None = None
    evidence: str | None = None
    unit_index: int | None = None
    source: str | None = Field(None, examples=["llm", "rule", "manual"])


class LineItemOut(ApiModel):
    id: uuid.UUID
    position: int
    description: str | None
    quantity: Money | None
    unit_price: Money | None
    amount: Money | None
    tax_rate: Money | None
    fields: dict[str, FieldOut]


class IssueOut(ApiModel):
    id: uuid.UUID
    extraction_id: uuid.UUID
    rule_code: RuleCode
    severity: Severity
    field_path: str | None
    message: str
    expected_value: str | None
    actual_value: str | None
    related_extraction_id: uuid.UUID | None
    resolved: bool
    resolved_at: UtcDateTime | None
    resolved_by: str | None
    resolution_note: str | None
    created_at: UtcDateTime


class ReconciliationOut(ApiModel):
    id: uuid.UUID
    counterpart_extraction_id: uuid.UUID
    match_method: MatchMethod
    status: ReconciliationStatus
    differences: list[dict[str, Any]]
    run_id: uuid.UUID | None
    created_at: UtcDateTime


class ExtractionOut(ApiModel):
    id: uuid.UUID
    document_id: uuid.UUID
    invoice_index: int
    unit_start: int = Field(description="First page / frame / sheet (0-based) of this invoice.")
    unit_end: int = Field(description="Last page / frame / sheet (0-based, inclusive).")
    version: int
    is_current: bool
    lock_version: int = Field(description="Send as If-Match when correcting this extraction.")
    document_type: str
    review_status: ReviewStatus
    reviewed_by: str | None
    reviewed_at: UtcDateTime | None
    review_note: str | None
    overall_confidence: float | None
    model_name: str | None
    prompt_version: str | None
    fields: dict[str, FieldOut]
    line_items: list[LineItemOut]
    issues: list[IssueOut] = Field(description="Active issues (resolved ones included, superseded ones not).")
    reconciliations: list[ReconciliationOut]
    created_at: UtcDateTime
    updated_at: UtcDateTime


class DocumentResults(ApiModel):
    document_id: uuid.UUID
    status: DocumentStatus
    extractions: list[ExtractionOut]


class ExtractionVersionOut(ApiModel):
    id: uuid.UUID
    version: int
    is_current: bool
    review_status: ReviewStatus
    overall_confidence: float | None
    model_name: str | None
    invoice_number: str | None
    total: Money | None
    created_at: UtcDateTime


class CorrectionOut(ApiModel):
    id: uuid.UUID
    extraction_id: uuid.UUID
    line_item_id: uuid.UUID | None
    field_path: str
    old_value: Any
    new_value: Any
    corrected_by: str
    reason: str | None
    created_at: UtcDateTime


Value = str | None


class CorrectionFields(ApiModel):
    """The ONLY header fields a correction may touch (anything else -> 422).
    Send null to clear a value; omit a field to leave it alone."""

    invoice_number: Value = Field(None, max_length=MAX_VALUE_CHARS)
    invoice_date: Value = Field(None, max_length=MAX_VALUE_CHARS, examples=["2026-03-01"])
    supplier_name: Value = Field(None, max_length=MAX_VALUE_CHARS)
    customer_name: Value = Field(None, max_length=MAX_VALUE_CHARS)
    subtotal: Value = Field(None, max_length=MAX_VALUE_CHARS, examples=["100.00"])
    tax: Value = Field(None, max_length=MAX_VALUE_CHARS)
    total: Value = Field(None, max_length=MAX_VALUE_CHARS, examples=["120.00"])
    currency: Value = Field(None, max_length=MAX_VALUE_CHARS, examples=["EUR"])


class LineItemOp(ApiModel):
    op: Literal["add", "update", "delete"]
    id: uuid.UUID | None = Field(None, description="Required for update and delete.")
    description: Value = Field(None, max_length=MAX_VALUE_CHARS)
    quantity: Value = Field(None, max_length=MAX_VALUE_CHARS)
    unit_price: Value = Field(None, max_length=MAX_VALUE_CHARS)
    amount: Value = Field(None, max_length=MAX_VALUE_CHARS)
    tax_rate: Value = Field(None, max_length=MAX_VALUE_CHARS, examples=["0.20"])


class CorrectionRequest(ApiModel):
    fields: CorrectionFields | None = None
    line_items: list[LineItemOp] = Field(default_factory=list, max_length=MAX_LINE_OPS)
    reason: str | None = Field(None, max_length=MAX_NOTE_CHARS)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "fields": {"total": "120.00", "invoice_date": "2026-03-01"},
                    "line_items": [
                        {"op": "update", "id": "3fa2b1c9-0000-4000-8000-000000000001", "amount": "60.00"}
                    ],
                    "reason": "Total misread by OCR",
                }
            ]
        }
    }


class ApproveRequest(ApiModel):
    note: str | None = Field(None, max_length=MAX_NOTE_CHARS)


class RejectRequest(ApiModel):
    note: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)


class ResolveRequest(ApiModel):
    note: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)
