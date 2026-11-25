"""SQL for extractions, their line items, issues, corrections and reconciliations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Correction, Extraction, Reconciliation, ValidationIssue
from app.models.enums import Severity


class ExtractionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, extraction_id: uuid.UUID) -> Extraction | None:
        return self._session.scalar(
            select(Extraction)
            .where(Extraction.id == extraction_id)
            .options(selectinload(Extraction.line_items))
        )

    def current_for_document(self, document_id: uuid.UUID) -> list[Extraction]:
        return list(
            self._session.scalars(
                select(Extraction)
                .where(Extraction.document_id == document_id, Extraction.is_current.is_(True))
                .options(selectinload(Extraction.line_items))
                .order_by(Extraction.invoice_index)
            )
        )

    def versions(self, extraction: Extraction) -> list[Extraction]:
        return list(
            self._session.scalars(
                select(Extraction)
                .where(
                    Extraction.document_id == extraction.document_id,
                    Extraction.invoice_index == extraction.invoice_index,
                )
                .order_by(Extraction.version.desc())
            )
        )

    def corrections(self, extraction_id: uuid.UUID) -> list[Correction]:
        return list(
            self._session.scalars(
                select(Correction)
                .where(Correction.extraction_id == extraction_id)
                .order_by(Correction.created_at, Correction.id)
            )
        )

    def active_issues(self, extraction_ids: Sequence[uuid.UUID]) -> list[ValidationIssue]:
        if not extraction_ids:
            return []
        return list(
            self._session.scalars(
                select(ValidationIssue)
                .where(
                    ValidationIssue.extraction_id.in_(extraction_ids), ValidationIssue.superseded_at.is_(None)
                )
                .order_by(ValidationIssue.created_at, ValidationIssue.id)
            )
        )

    def reconciliations(self, extraction_ids: Sequence[uuid.UUID]) -> list[Reconciliation]:
        if not extraction_ids:
            return []
        return list(
            self._session.scalars(
                select(Reconciliation)
                .where(
                    or_(
                        Reconciliation.left_extraction_id.in_(extraction_ids),
                        Reconciliation.right_extraction_id.in_(extraction_ids),
                    )
                )
                .order_by(Reconciliation.created_at, Reconciliation.id)
            )
        )

    def has_open_errors(self, extraction_id: uuid.UUID) -> bool:
        return (
            self._session.scalar(
                select(ValidationIssue.id)
                .where(
                    ValidationIssue.extraction_id == extraction_id,
                    ValidationIssue.superseded_at.is_(None),
                    ValidationIssue.resolved_at.is_(None),
                    ValidationIssue.severity == Severity.ERROR,
                )
                .limit(1)
            )
            is not None
        )

    def issue(self, issue_id: uuid.UUID) -> ValidationIssue | None:
        return self._session.get(ValidationIssue, issue_id)
