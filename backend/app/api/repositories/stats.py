"""Aggregate queries for GET /stats (admin). All grouped in SQL, nothing loaded row by row."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from app.models import Document, Extraction, ValidationIssue
from app.models.enums import DocumentStatus

P50: Final = 0.5
P95: Final = 0.95
MS_PER_S: Final = 1000
FINISHED: Final = (DocumentStatus.COMPLETED, DocumentStatus.NEEDS_REVIEW)


class StatsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def documents_by_status(self) -> dict[str, int]:
        rows = self._session.execute(
            select(Document.status, func.count())
            .where(Document.deleted_at.is_(None))
            .group_by(Document.status)
        ).all()
        return {str(status): int(count) for status, count in rows}

    def extractions_by_review_status(self) -> dict[str, int]:
        rows = self._session.execute(
            select(Extraction.review_status, func.count())
            .join(Document, Document.id == Extraction.document_id)
            .where(Extraction.is_current.is_(True), Document.deleted_at.is_(None))
            .group_by(Extraction.review_status)
        ).all()
        return {str(status): int(count) for status, count in rows}

    def open_issues_by_rule(self) -> dict[str, int]:
        rows = self._session.execute(
            select(ValidationIssue.rule_code, func.count())
            .join(Extraction, Extraction.id == ValidationIssue.extraction_id)
            .where(
                Extraction.is_current.is_(True),
                ValidationIssue.superseded_at.is_(None),
                ValidationIssue.resolved_at.is_(None),
            )
            .group_by(ValidationIssue.rule_code)
            .order_by(func.count().desc())
        ).all()
        return {str(code): int(count) for code, count in rows}

    def processing_ms_by_mime(self) -> dict[str, tuple[int, float | None, float | None]]:
        """mime -> (count, p50 ms, p95 ms) over finished documents."""
        seconds = extract("epoch", Document.processing_finished_at - Document.processing_started_at)
        rows = self._session.execute(
            select(
                Document.mime_type,
                func.count(),
                func.percentile_cont(P50).within_group(seconds),
                func.percentile_cont(P95).within_group(seconds),
            )
            .where(
                Document.status.in_(FINISHED),
                Document.processing_started_at.is_not(None),
                Document.processing_finished_at.is_not(None),
                Document.deleted_at.is_(None),
            )
            .group_by(Document.mime_type)
        ).all()
        return {mime: (int(count), _ms(p50), _ms(p95)) for mime, count, p50, p95 in rows}

    def throughput_since(self, since: datetime) -> tuple[int, int, int]:
        """(uploaded, finished, failed) since `since`."""
        uploaded = self._session.scalar(select(func.count()).where(Document.created_at >= since)) or 0
        finished = (
            self._session.scalar(
                select(func.count()).where(
                    Document.processing_finished_at >= since, Document.status.in_(FINISHED)
                )
            )
            or 0
        )
        failed = (
            self._session.scalar(
                select(func.count()).where(
                    Document.processing_finished_at >= since, Document.status == DocumentStatus.FAILED
                )
            )
            or 0
        )
        return int(uploaded), int(finished), int(failed)


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else float(seconds) * MS_PER_S
