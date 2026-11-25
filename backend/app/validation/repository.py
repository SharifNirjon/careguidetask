"""DB edge for validation: snapshots in, candidates in, issues/reconciliations out.

Rules never touch the database; everything they need is loaded here first.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from app.config import ValidationSettings
from app.models import Document, DocumentStatus, Extraction, Reconciliation, ReviewStatus, ValidationIssue
from app.models.enums import MatchMethod
from app.pipeline.types import FileKind
from app.validation.crossdoc import KeyComparison, within_days
from app.validation.lifecycle import ActiveIssue, IssuePlan
from app.validation.policy import document_status
from app.validation.types import (
    Candidate,
    DocumentInfo,
    ExtractionSnapshot,
    LineSnapshot,
    MatchKind,
    SourceKind,
)

LOCK_NAMESPACE: Final = "validation"
STATUS_OWNED_BY_VALIDATION: Final = frozenset({DocumentStatus.COMPLETED, DocumentStatus.NEEDS_REVIEW})
RECONCILED_COLUMNS: Final = ("status", "differences", "match_key", "match_method")


def to_snapshot(extraction: Extraction) -> ExtractionSnapshot:
    meta = extraction.field_meta or {}
    return ExtractionSnapshot(
        id=extraction.id,
        document_id=extraction.document_id,
        version=extraction.version,
        document_type=str(meta.get("_document_type") or "invoice"),
        invoice_number=extraction.invoice_number,
        invoice_date=extraction.invoice_date,
        supplier_name=extraction.supplier_name,
        customer_name=extraction.customer_name,
        subtotal=extraction.subtotal,
        tax=extraction.tax,
        total=extraction.total,
        currency=extraction.currency.strip() if extraction.currency else None,
        invoice_number_norm=extraction.invoice_number_norm,
        supplier_name_norm=extraction.supplier_name_norm,
        overall_confidence=float(extraction.overall_confidence)
        if extraction.overall_confidence is not None
        else None,
        field_meta=meta,
        line_items=tuple(
            LineSnapshot(
                index=i.position,
                description=i.description,
                quantity=i.quantity,
                unit_price=i.unit_price,
                amount=i.amount,
                field_meta=i.field_meta or {},
            )
            for i in sorted(extraction.line_items, key=lambda li: li.position)
        ),
    )


def to_document_info(document: Document) -> DocumentInfo:
    kind = FileKind.from_mime(document.mime_type)
    return DocumentInfo(
        id=document.id,
        source_kind=SourceKind.SPREADSHEET if kind.is_tabular else SourceKind.DOCUMENT,
        created_at=document.created_at,
        sha256=document.sha256,
        filename=document.original_filename,
    )


def lock_key(snapshot: ExtractionSnapshot) -> str:
    """Two copies of one invoice share their invoice number: serialize on it.
    Without a number, the supplier key; without either, the extraction itself."""
    return f"{LOCK_NAMESPACE}:{snapshot.invoice_number_norm or snapshot.supplier_name_norm or snapshot.id}"


@dataclass(frozen=True, slots=True)
class Loaded:
    snapshot: ExtractionSnapshot
    document: DocumentInfo
    review_status: ReviewStatus


class ValidationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock(self, key: str) -> None:
        """Transaction-scoped: released at commit/rollback, never leaked."""
        self._session.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))

    def load(self, extraction_id: uuid.UUID) -> Loaded:
        extraction = self._session.scalars(
            select(Extraction)
            .where(Extraction.id == extraction_id)
            .options(selectinload(Extraction.line_items))
        ).one()
        document = self._session.get_one(Document, extraction.document_id)
        return Loaded(to_snapshot(extraction), to_document_info(document), extraction.review_status)

    def find_candidates(self, snapshot: ExtractionSnapshot, settings: ValidationSettings) -> list[Candidate]:
        """Current extractions of OTHER live documents that may be the same invoice.

        `supplier_name_norm % :name` is served by the trigram GIN index; the
        threshold is set transaction-locally so `%` means similarity >= SUPPLIER_MATCH_MIN.
        """
        supplier = snapshot.supplier_name_norm
        if not supplier:
            return []
        self._session.execute(
            text("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"),
            {"t": str(settings.supplier_match_min)},
        )
        matches = []
        if snapshot.invoice_number_norm:
            matches.append(Extraction.invoice_number_norm == snapshot.invoice_number_norm)
        if snapshot.total is not None and snapshot.invoice_date is not None:
            window = timedelta(days=settings.duplicate_date_window_days)
            matches.append(
                and_(
                    Extraction.total == snapshot.total,
                    Extraction.invoice_date.between(
                        snapshot.invoice_date - window, snapshot.invoice_date + window
                    ),
                )
            )
        if not matches:
            return []
        similarity = func.similarity(Extraction.supplier_name_norm, supplier).label("similarity")
        rows = self._session.execute(
            select(Extraction, Document, similarity)
            .join(Document, Document.id == Extraction.document_id)
            .where(
                Extraction.is_current.is_(True),
                Extraction.document_id != snapshot.document_id,
                Document.deleted_at.is_(None),
                Extraction.supplier_name_norm.op("%")(supplier),
                or_(*matches),
            )
            .options(selectinload(Extraction.line_items))
            .order_by(Document.created_at, Document.id)
        ).all()
        candidates = []
        for extraction, document, score in rows:
            other = to_snapshot(extraction)
            if snapshot.invoice_number_norm and other.invoice_number_norm == snapshot.invoice_number_norm:
                kind = MatchKind.EXACT
            elif within_days(snapshot, other, settings.duplicate_date_window_days):
                kind = MatchKind.FUZZY
            else:
                continue
            candidates.append(Candidate(other, to_document_info(document), float(score), kind))
        return candidates

    def active_issues(self, extraction_id: uuid.UUID) -> list[ActiveIssue]:
        rows = self._session.scalars(
            select(ValidationIssue).where(
                ValidationIssue.extraction_id == extraction_id, ValidationIssue.superseded_at.is_(None)
            )
        )
        return [
            ActiveIssue(
                r.id,
                r.rule_code.value,
                r.fingerprint,
                r.severity,
                r.message,
                r.expected_value,
                r.actual_value,
                r.resolved_at is not None,
            )
            for r in rows
        ]

    def apply(self, extraction_id: uuid.UUID, plan: IssuePlan, now: datetime) -> None:
        for issue in plan.insert:
            self._session.add(
                ValidationIssue(
                    extraction_id=extraction_id,
                    rule_code=issue.rule_code,
                    severity=issue.severity,
                    field_path=issue.field_path,
                    message=issue.message,
                    expected_value=issue.expected_value,
                    actual_value=issue.actual_value,
                    related_extraction_id=issue.related_extraction_id,
                    fingerprint=issue.fingerprint,
                )
            )
        for change in plan.update:
            values: dict[str, object] = {
                "severity": change.issue.severity,
                "message": change.issue.message,
                "expected_value": change.issue.expected_value,
                "actual_value": change.issue.actual_value,
            }
            if change.reopen:
                values |= {"resolved_at": None, "resolved_by": None, "resolution_note": None}
            self._session.execute(
                update(ValidationIssue).where(ValidationIssue.id == change.id).values(**values)
            )
        if plan.supersede:
            self._session.execute(
                update(ValidationIssue)
                .where(ValidationIssue.id.in_(plan.supersede))
                .values(superseded_at=now)
            )
        self._session.flush()

    def upsert_reconciliation(
        self,
        this: ExtractionSnapshot,
        other: Candidate,
        comparison: KeyComparison,
        *,
        run_id: uuid.UUID | None = None,
    ) -> None:
        """One row per pair (canonical order). Untouched when nothing changed, so
        re-validating an unchanged extraction writes no rows."""
        number = this.invoice_number_norm or other.extraction.invoice_number_norm
        record = Reconciliation.between(
            this.id,
            other.extraction.id,
            differences=[d.to_dict() for d in comparison.differences],
            match_key=f"{this.supplier_name_norm}|{number}",
            match_method=MatchMethod.INVOICE_NUMBER if other.kind is MatchKind.EXACT else MatchMethod.FUZZY,
            status=comparison.status,
            run_id=run_id,
        )
        values = {
            "left_extraction_id": record.left_extraction_id,
            "right_extraction_id": record.right_extraction_id,
            "match_key": record.match_key,
            "match_method": record.match_method,
            "status": record.status,
            "differences": record.differences,
            "run_id": run_id,
        }
        stmt = pg_insert(Reconciliation).values(**values)
        excluded = stmt.excluded
        set_ = {c: excluded[c] for c in RECONCILED_COLUMNS}
        if run_id is not None:
            set_["run_id"] = excluded["run_id"]
        current = Reconciliation.__table__.c
        differs = or_(*(current[c].is_distinct_from(excluded[c]) for c in set_))
        self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["left_extraction_id", "right_extraction_id"], set_=set_, where=differs
            )
        )

    def set_review_status(self, extraction_id: uuid.UUID, status: ReviewStatus) -> bool:
        """Only writes on change (keeps lock_version and updated_at stable otherwise)."""
        extraction = self._session.get_one(Extraction, extraction_id)
        if extraction.review_status is status:
            return False
        extraction.review_status = status
        self._session.flush()
        return True

    def current_entries(
        self, document_ids: Sequence[uuid.UUID]
    ) -> list[tuple[ExtractionSnapshot, DocumentInfo]]:
        """Current extractions of live documents, for batch reconciliation."""
        if not document_ids:
            return []
        rows = self._session.execute(
            select(Extraction, Document)
            .join(Document, Document.id == Extraction.document_id)
            .where(
                Extraction.document_id.in_(document_ids),
                Extraction.is_current.is_(True),
                Document.deleted_at.is_(None),
            )
            .options(selectinload(Extraction.line_items))
            .order_by(Document.created_at, Extraction.invoice_index)
        ).all()
        return [(to_snapshot(extraction), to_document_info(document)) for extraction, document in rows]

    def current_extraction_ids(self, document_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            self._session.scalars(
                select(Extraction.id)
                .where(Extraction.document_id == document_id, Extraction.is_current.is_(True))
                .order_by(Extraction.invoice_index)
            )
        )

    def refresh_document_status(self, document_id: uuid.UUID) -> DocumentStatus:
        """Recompute from current extractions; only touches documents that finished
        processing (queued/processing/failed belong to the earlier stages)."""
        statuses = self._session.scalars(
            select(Extraction.review_status).where(
                Extraction.document_id == document_id, Extraction.is_current.is_(True)
            )
        )
        status = document_status(statuses)
        document = self._session.get_one(Document, document_id)
        if document.status in STATUS_OWNED_BY_VALIDATION and document.status is not status:
            document.status = status
        return status
