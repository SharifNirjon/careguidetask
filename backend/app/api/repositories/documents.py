"""SQL for documents: filtered/sorted keyset listing and detail lookups.

Filters on invoice fields match ANY current extraction of the document.
Sorting by invoice_date/total uses the document's FIRST invoice (index 0);
NULLs always sort last. Keyset: (sort value, created_at, id).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, Select, and_, exists, func, literal, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from app.api.services.filters import DocumentFilters, SortField, SortOrder
from app.extraction.normalize import invoice_number_norm, supplier_name_norm
from app.models import (
    Document,
    DocumentUnit,
    Extraction,
    ProcessingEvent,
    ProcessingJob,
    ValidationIssue,
)
from app.models.enums import JobStatus, Severity

# Stand-ins for NULL so missing values sort last in either direction.
NULL_DATE_HIGH: Final = date(9999, 12, 31)
NULL_DATE_LOW: Final = date(1, 1, 1)
NULL_TOTAL_HIGH: Final = Decimal("99999999999999.9999")
NULL_TOTAL_LOW: Final = -NULL_TOTAL_HIGH
ACTIVE_JOB_STATUSES: Final = (JobStatus.QUEUED, JobStatus.RUNNING)


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
        conditions.append(e.customer_name.ilike(f"%{escape_like(f.customer)}%", escape="\\"))
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


@dataclass(frozen=True, slots=True)
class ListedDocument:
    document: Document
    sort_value: Any


@dataclass(frozen=True, slots=True)
class IssueCounts:
    error: int = 0
    warning: int = 0
    info: int = 0


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, document_id: uuid.UUID) -> Document | None:
        doc = self._session.get(Document, document_id)
        return doc if doc is not None and doc.deleted_at is None else None

    def list_page(
        self,
        f: DocumentFilters,
        *,
        sort: SortField,
        order: SortOrder,
        limit: int,
        after: tuple[Any, ...] | None,
    ) -> list[ListedDocument]:
        primary = aliased(Extraction)
        sort_expr = self._sort_expr(primary, sort, order)
        query: Select[Any] = (
            select(Document, sort_expr.label("sort_value"))
            .outerjoin(
                primary,
                and_(
                    primary.document_id == Document.id,
                    primary.is_current.is_(True),
                    primary.invoice_index == 0,
                ),
            )
            .where(*document_conditions(f))
        )
        key = tuple_(sort_expr, Document.created_at, Document.id)
        if after is not None:
            query = query.where(
                key > tuple_(*map(literal, after))
                if order is SortOrder.ASC
                else key < tuple_(*map(literal, after))
            )
        columns = (sort_expr, Document.created_at, Document.id)
        query = query.order_by(*(c.asc() if order is SortOrder.ASC else c.desc() for c in columns))
        rows = self._session.execute(query.limit(limit)).all()
        return [ListedDocument(row[0], row[1]) for row in rows]

    @staticmethod
    def _sort_expr(primary: Any, sort: SortField, order: SortOrder) -> ColumnElement[Any]:
        asc = order is SortOrder.ASC
        if sort is SortField.INVOICE_DATE:
            return func.coalesce(primary.invoice_date, NULL_DATE_HIGH if asc else NULL_DATE_LOW)
        if sort is SortField.TOTAL:
            return func.coalesce(primary.total, NULL_TOTAL_HIGH if asc else NULL_TOTAL_LOW)
        return Document.created_at.expression

    def current_extractions(self, document_ids: Sequence[uuid.UUID]) -> list[Extraction]:
        if not document_ids:
            return []
        return list(
            self._session.scalars(
                select(Extraction)
                .where(Extraction.document_id.in_(document_ids), Extraction.is_current.is_(True))
                .order_by(Extraction.document_id, Extraction.invoice_index)
            )
        )

    def open_issue_counts(self, extraction_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, IssueCounts]:
        if not extraction_ids:
            return {}
        rows = self._session.execute(
            select(ValidationIssue.extraction_id, ValidationIssue.severity, func.count())
            .where(
                ValidationIssue.extraction_id.in_(extraction_ids),
                ValidationIssue.superseded_at.is_(None),
                ValidationIssue.resolved_at.is_(None),
            )
            .group_by(ValidationIssue.extraction_id, ValidationIssue.severity)
        ).all()
        counts: dict[uuid.UUID, dict[str, int]] = {}
        for extraction_id, severity, count in rows:
            counts.setdefault(extraction_id, {})[Severity(severity).value] = int(count)
        return {k: IssueCounts(**v) for k, v in counts.items()}

    def latest_job(self, document_id: uuid.UUID) -> ProcessingJob | None:
        return self._session.scalar(
            select(ProcessingJob)
            .where(ProcessingJob.document_id == document_id)
            .order_by(ProcessingJob.queued_at.desc(), ProcessingJob.id.desc())
            .limit(1)
        )

    def job_events(self, job_id: uuid.UUID) -> list[ProcessingEvent]:
        return list(
            self._session.scalars(
                select(ProcessingEvent).where(ProcessingEvent.job_id == job_id).order_by(ProcessingEvent.id)
            )
        )

    def unit_count(self, document_id: uuid.UUID) -> int:
        return int(
            self._session.scalar(select(func.count()).where(DocumentUnit.document_id == document_id)) or 0
        )

    def events(
        self, document_id: uuid.UUID, *, limit: int, after: tuple[Any, ...] | None
    ) -> list[ProcessingEvent]:
        query = select(ProcessingEvent).where(ProcessingEvent.document_id == document_id)
        if after is not None:
            query = query.where(
                tuple_(ProcessingEvent.created_at, ProcessingEvent.id)
                > tuple_(literal(after[0]), literal(after[1]))
            )
        return list(
            self._session.scalars(query.order_by(ProcessingEvent.created_at, ProcessingEvent.id).limit(limit))
        )

    def unit(self, document_id: uuid.UUID, unit_index: int) -> DocumentUnit | None:
        return self._session.scalar(
            select(DocumentUnit)
            .where(DocumentUnit.document_id == document_id, DocumentUnit.unit_index == unit_index)
            .order_by(DocumentUnit.unit_type)
            .limit(1)
        )

    def soft_delete(self, document: Document, now: datetime) -> None:
        document.deleted_at = now
