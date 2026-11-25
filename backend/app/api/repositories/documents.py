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

from sqlalchemy import ColumnElement, Select, and_, func, literal, select, tuple_
from sqlalchemy.orm import Session, aliased

from app.api.services.filters import SortField, SortOrder
from app.models import (
    Document,
    DocumentUnit,
    Extraction,
    ProcessingEvent,
    ProcessingJob,
    ValidationIssue,
)
from app.models.enums import Severity
from app.search.filters import DocumentFilters, document_conditions

# Stand-ins for NULL so missing values sort last in either direction.
NULL_DATE_HIGH: Final = date(9999, 12, 31)
NULL_DATE_LOW: Final = date(1, 1, 1)
NULL_TOTAL_HIGH: Final = Decimal("99999999999999.9999")
NULL_TOTAL_LOW: Final = -NULL_TOTAL_HIGH


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
