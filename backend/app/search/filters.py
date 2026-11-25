"""Document filters: plain data plus the SQL that applies them.

Shared by GET /documents and every search mode, so a filter means exactly the
same thing everywhere. Invoice-field filters match ANY current extraction of
the document.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.extraction.normalize import invoice_number_norm, supplier_name_norm
from app.models import Document, Extraction, ValidationIssue
from app.models.enums import DocumentStatus, ReviewStatus, RuleCode
from app.pipeline.types import FileKind


@dataclass(frozen=True, slots=True)
class DocumentFilters:
    status: DocumentStatus | None = None
    review_status: ReviewStatus | None = None
    file_type: FileKind | None = None
    supplier: str | None = None
    customer: str | None = None
    invoice_number: str | None = None
    invoice_date_from: date | None = None
    invoice_date_to: date | None = None
    total_min: Decimal | None = None
    total_max: Decimal | None = None
    currency: str | None = None
    has_open_issues: bool | None = None
    rule_code: RuleCode | None = None
    uploaded_from: datetime | None = None
    uploaded_to: datetime | None = None

    @property
    def touches_extractions(self) -> bool:
        return any(
            v is not None
            for v in (
                self.review_status,
                self.supplier,
                self.customer,
                self.invoice_number,
                self.invoice_date_from,
                self.invoice_date_to,
                self.total_min,
                self.total_max,
                self.currency,
            )
        )


def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def open_issue_exists(document_id: Any, rule_code: Any | None = None) -> ColumnElement[bool]:
    """An active, unresolved issue on a current extraction of the document."""
    e = aliased(Extraction)
    conditions = [
        ValidationIssue.extraction_id == e.id,
        e.document_id == document_id,
        e.is_current.is_(True),
        ValidationIssue.superseded_at.is_(None),
        ValidationIssue.resolved_at.is_(None),
    ]
    if rule_code is not None:
        conditions.append(ValidationIssue.rule_code == rule_code)
    return exists(
        select(ValidationIssue.id).join(e, ValidationIssue.extraction_id == e.id).where(*conditions)
    )


def extraction_conditions(e: Any, f: DocumentFilters) -> list[ColumnElement[bool]]:
    """Filters that apply to one extraction row `e` (shared with search)."""
    conditions: list[ColumnElement[bool]] = []
    if f.review_status is not None:
        conditions.append(e.review_status == f.review_status)
    if f.supplier:
        key = supplier_name_norm(f.supplier) or f.supplier.casefold()
        conditions.append(
            or_(
                e.supplier_name_norm.op("%")(key),
                e.supplier_name_norm.ilike(f"%{escape_like(key)}%", escape="\\"),
            )
        )
    if f.customer:
        # lower(...) LIKE: served by the trigram index on lower(customer_name)
        pattern = f"%{escape_like(f.customer.casefold())}%"
        conditions.append(func.lower(e.customer_name).like(pattern, escape="\\"))
    if f.invoice_number:
        conditions.append(e.invoice_number_norm == (invoice_number_norm(f.invoice_number) or ""))
    if f.invoice_date_from:
        conditions.append(e.invoice_date >= f.invoice_date_from)
    if f.invoice_date_to:
        conditions.append(e.invoice_date <= f.invoice_date_to)
    if f.total_min is not None:
        conditions.append(e.total >= f.total_min)
    if f.total_max is not None:
        conditions.append(e.total <= f.total_max)
    if f.currency:
        conditions.append(e.currency == f.currency.upper())
    return conditions


def document_conditions(f: DocumentFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [Document.deleted_at.is_(None)]
    if f.status is not None:
        conditions.append(Document.status == f.status)
    if f.file_type is not None:
        conditions.append(Document.mime_type == f.file_type.mime_type)
    if f.uploaded_from is not None:
        conditions.append(Document.created_at >= f.uploaded_from)
    if f.uploaded_to is not None:
        conditions.append(Document.created_at <= f.uploaded_to)
    if f.has_open_issues is not None:
        has = open_issue_exists(Document.id)
        conditions.append(has if f.has_open_issues else ~has)
    if f.rule_code is not None:
        conditions.append(open_issue_exists(Document.id, f.rule_code))
    if f.touches_extractions:
        e = aliased(Extraction)
        conditions.append(
            exists(
                select(e.id).where(
                    e.document_id == Document.id, e.is_current.is_(True), *extraction_conditions(e, f)
                )
            )
        )
    return conditions
