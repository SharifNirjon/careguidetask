"""extractions, line_items, validation_issues, corrections, reconciliations."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    Confidence,
    Money,
    Rate,
    StrEnumText,
    confidence_check,
    created_at_col,
    enum_check,
    updated_at_col,
    uuid_pk,
)
from app.models.enums import (
    MatchMethod,
    ReconciliationStatus,
    ReviewStatus,
    RuleCode,
    RunStatus,
    Severity,
)

if TYPE_CHECKING:
    from app.models.document import Document


class Extraction(Base):
    """One invoice found in a document, one row per extraction run (versioned).

    Exactly one row per (document_id, invoice_index) has is_current = true.
    Create new versions with app.db.versioning.add_extraction_version().
    All invoice fields are nullable: missing values are NULL, never guessed.
    """

    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    invoice_index: Mapped[int] = mapped_column(Integer)
    unit_start: Mapped[int] = mapped_column(Integer)
    unit_end: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    invoice_number: Mapped[str | None]
    invoice_date: Mapped[date | None] = mapped_column(Date)
    supplier_name: Mapped[str | None]
    customer_name: Mapped[str | None]
    subtotal: Mapped[Decimal | None] = mapped_column(Money)
    tax: Mapped[Decimal | None] = mapped_column(Money)
    total: Mapped[Decimal | None] = mapped_column(Money)  # may be negative (credit notes)
    currency: Mapped[str | None] = mapped_column(CHAR(3))

    # Normalized match keys, filled by app code (extraction/normalizers.py).
    invoice_number_norm: Mapped[str | None]
    supplier_name_norm: Mapped[str | None]

    # {field: {confidence, evidence_text, unit_index, source: llm|rule|manual, grounded}}
    field_meta: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    overall_confidence: Mapped[Decimal | None] = mapped_column(Confidence)
    model_name: Mapped[str | None]
    prompt_version: Mapped[str | None]
    raw_llm_response: Mapped[dict[str, Any] | None]
    token_usage: Mapped[dict[str, Any] | None]
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    review_status: Mapped[ReviewStatus] = mapped_column(
        StrEnumText(ReviewStatus), server_default=ReviewStatus.PENDING.value
    )
    reviewed_by: Mapped[str | None]
    reviewed_at: Mapped[datetime | None]
    # Optimistic lock: every ORM UPDATE adds "AND lock_version = :old" and bumps it.
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The job that produced this version: makes a retried job idempotent.
    processing_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    document: Mapped[Document] = relationship(back_populates="extractions")
    line_items: Mapped[list[LineItem]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: LineItem.position,
    )
    validation_issues: Mapped[list[ValidationIssue]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys=lambda: [ValidationIssue.extraction_id],
    )
    corrections: Mapped[list[Correction]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: Correction.created_at,
    )

    __mapper_args__ = {"version_id_col": lock_version}

    __table_args__ = (
        # Leading document_id also serves as the FK index.
        UniqueConstraint("document_id", "invoice_index", "version"),
        UniqueConstraint("id", "document_id"),  # target for chunks' composite FK
        Index(
            "uq_extractions_document_id_invoice_index_current",
            "document_id",
            "invoice_index",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_extractions_supplier_name_norm_invoice_number_norm",
            "supplier_name_norm",
            "invoice_number_norm",
        ),
        Index(
            "ix_extractions_supplier_name_norm_trgm",
            "supplier_name_norm",
            postgresql_using="gin",
            postgresql_ops={"supplier_name_norm": "gin_trgm_ops"},
        ),
        Index("ix_extractions_invoice_date", "invoice_date"),
        Index("ix_extractions_total", "total"),
        Index("ix_extractions_review_status", "review_status"),
        CheckConstraint("invoice_index >= 0", name="invoice_index_nonneg"),
        CheckConstraint("unit_start >= 0", name="unit_start_nonneg"),
        CheckConstraint("unit_end >= unit_start", name="unit_range"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso4217"),
        CheckConstraint("latency_ms >= 0", name="latency_ms_nonneg"),
        confidence_check("overall_confidence"),
        enum_check("review_status", ReviewStatus),
        # Deliberately NO CHECK (total >= 0): credit notes are negative.
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extractions.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None]
    quantity: Mapped[Decimal | None] = mapped_column(Money)
    unit_price: Mapped[Decimal | None] = mapped_column(Money)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    tax_rate: Mapped[Decimal | None] = mapped_column(Rate)
    field_meta: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))

    extraction: Mapped[Extraction] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("extraction_id", "position"),  # also the FK index
        CheckConstraint("position >= 0", name="position_nonneg"),
    )


def issue_fingerprint(rule_code: str, field_path: str | None, related_extraction_id: object) -> str:
    """Identity of an issue across re-validations of ONE extraction.

    Must stay byte-for-byte identical to the SQL backfill in migration 0004:
    sha256(rule_code || '|' || coalesce(field_path, '') || '|' || coalesce(related::text, '')).
    """
    related = "" if related_extraction_id is None else str(related_extraction_id)
    body = f"{rule_code}|{field_path or ''}|{related}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _fingerprint_default(context: Any) -> str:
    params = context.get_current_parameters()
    return issue_fingerprint(
        str(params["rule_code"]), params.get("field_path"), params.get("related_extraction_id")
    )


class ValidationIssue(Base):
    """A rule violation. Never deleted: re-validation sets superseded_at instead."""

    __tablename__ = "validation_issues"

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), index=True
    )
    rule_code: Mapped[RuleCode] = mapped_column(StrEnumText(RuleCode))
    severity: Mapped[Severity] = mapped_column(StrEnumText(Severity))
    field_path: Mapped[str | None]  # e.g. "total", "line_items[2].amount"
    message: Mapped[str]
    expected_value: Mapped[str | None]
    actual_value: Mapped[str | None]
    related_extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extractions.id", ondelete="SET NULL"), index=True
    )
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[str | None]
    resolution_note: Mapped[str | None]
    superseded_at: Mapped[datetime | None]
    # sha256(rule_code, field_path, related_extraction_id): lets re-validation keep
    # an unchanged issue (and its resolution) instead of re-creating it.
    fingerprint: Mapped[str] = mapped_column(default=_fingerprint_default)
    created_at: Mapped[datetime] = created_at_col()

    extraction: Mapped[Extraction] = relationship(
        back_populates="validation_issues", foreign_keys=[extraction_id]
    )
    related_extraction: Mapped[Extraction | None] = relationship(foreign_keys=[related_extraction_id])

    __table_args__ = (
        # Fast "open issues for this extraction". The full extraction_id index
        # (index=True above) is still required: ON DELETE CASCADE must also find
        # superseded rows, which a partial index cannot serve.
        Index(
            "ix_validation_issues_extraction_id_active",
            "extraction_id",
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_validation_issues_rule_code_severity", "rule_code", "severity"),
        # One ACTIVE issue per fingerprint: the backstop for concurrent re-validation.
        Index(
            "uq_validation_issues_extraction_id_fingerprint_active",
            "extraction_id",
            "fingerprint",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        CheckConstraint("related_extraction_id <> extraction_id", name="related_not_self"),
        enum_check("rule_code", RuleCode),
        enum_check("severity", Severity),
    )


class Correction(Base):
    """Append-only audit of manual edits (UPDATE blocked by trigger).

    old_value/new_value are JSONB so any field type (string, number as string,
    date, whole line item) round-trips; store Decimals as strings to keep precision.
    """

    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), index=True
    )
    # SET NULL (not CASCADE) so deleting a line item keeps the audit trail;
    # field_path still identifies what was changed.
    line_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("line_items.id", ondelete="SET NULL"), index=True
    )
    field_path: Mapped[str]
    old_value: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    new_value: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    corrected_by: Mapped[str]
    reason: Mapped[str | None]
    created_at: Mapped[datetime] = created_at_col()

    extraction: Mapped[Extraction] = relationship(back_populates="corrections")


class Reconciliation(Base):
    """Cross-document verification between two extractions (PDF<->PDF, PDF<->Excel).

    The pair is stored in canonical order (left < right); use Reconciliation.between().
    """

    __tablename__ = "reconciliations"

    id: Mapped[uuid.UUID] = uuid_pk()
    left_extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extractions.id", ondelete="CASCADE"))
    right_extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), index=True
    )
    match_key: Mapped[str]
    match_method: Mapped[MatchMethod] = mapped_column(StrEnumText(MatchMethod))
    status: Mapped[ReconciliationStatus] = mapped_column(StrEnumText(ReconciliationStatus))
    # [{"field": "total", "left": "100.0000", "right": "110.0000"}, ...]
    differences: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))
    # Last batch run that compared this pair (a pair has one row; each run's full
    # report lives in reconciliation_runs.summary).
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reconciliation_runs.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = created_at_col()

    left_extraction: Mapped[Extraction] = relationship(foreign_keys=[left_extraction_id])
    right_extraction: Mapped[Extraction] = relationship(foreign_keys=[right_extraction_id])

    __table_args__ = (
        UniqueConstraint("left_extraction_id", "right_extraction_id"),  # also left FK index
        CheckConstraint("left_extraction_id < right_extraction_id", name="canonical_pair_order"),
        enum_check("match_method", MatchMethod),
        enum_check("status", ReconciliationStatus),
    )

    @classmethod
    def between(
        cls, a: uuid.UUID, b: uuid.UUID, *, differences: list[Any] | None = None, **kw: Any
    ) -> Reconciliation:
        """Build a reconciliation with the pair in canonical order.

        Python's uuid.UUID ordering (128-bit int) matches PostgreSQL's uuid
        ordering (bytewise), so this always satisfies the CHECK. When the pair is
        swapped, "left"/"right" inside `differences` are swapped too.
        """
        if a == b:
            raise ValueError("cannot reconcile an extraction with itself")
        diffs = list(differences or [])
        if b < a:
            a, b = b, a
            diffs = [{**d, "left": d.get("right"), "right": d.get("left")} for d in diffs]
        return cls(left_extraction_id=a, right_extraction_id=b, differences=diffs, **kw)


class ReconciliationRun(Base):
    """A user-requested batch check: one set of documents against another
    (PDFs vs an Excel ledger, or PDFs vs PDFs). The report is in `summary`."""

    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    left_document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    right_document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    status: Mapped[RunStatus] = mapped_column(StrEnumText(RunStatus), server_default=RunStatus.RUNNING.value)
    summary: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_by: Mapped[str | None]
    created_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None]

    __table_args__ = (enum_check("status", RunStatus),)
