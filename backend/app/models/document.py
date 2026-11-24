"""documents and document_units."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    Confidence,
    StrEnumText,
    confidence_check,
    created_at_col,
    enum_check,
    updated_at_col,
    uuid_pk,
)
from app.models.enums import DocumentStatus, ErrorCode, ExtractionMethod, UnitType

if TYPE_CHECKING:
    from app.models.extraction import Extraction
    from app.models.processing import ProcessingJob


class Document(Base):
    """One uploaded file. Soft-deleted via deleted_at; hard delete cascades."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    original_filename: Mapped[str]
    storage_key: Mapped[str] = mapped_column(unique=True)
    mime_type: Mapped[str]  # sniffed from bytes, not trusted from the client
    file_ext: Mapped[str | None]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    status: Mapped[DocumentStatus] = mapped_column(
        StrEnumText(DocumentStatus), server_default=DocumentStatus.UPLOADED.value
    )
    error_code: Mapped[ErrorCode | None] = mapped_column(StrEnumText(ErrorCode))
    error_message: Mapped[str | None]
    page_count: Mapped[int | None] = mapped_column(Integer)
    duplicate_of_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    uploaded_by: Mapped[str | None]
    processing_started_at: Mapped[datetime | None]
    processing_finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None]
    # Invoice boundaries found by the segmenter: [{unit_start, unit_end, confidence, reasons}]
    segments: Mapped[list[Any] | None]

    units: Mapped[list[DocumentUnit]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: [DocumentUnit.unit_type, DocumentUnit.unit_index],
    )
    extractions: Mapped[list[Extraction]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    duplicate_of: Mapped[Document | None] = relationship(remote_side=[id])

    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("page_count >= 0", name="page_count_nonneg"),
        CheckConstraint("duplicate_of_document_id <> id", name="not_duplicate_of_self"),
        CheckConstraint(
            "processing_finished_at >= processing_started_at", name="processing_window"
        ),
        CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL", name="failed_has_error_code"
        ),
        enum_check("status", DocumentStatus),
        enum_check("error_code", ErrorCode),
        # Non-unique on purpose: duplicate uploads are stored and flagged, not rejected.
        Index("ix_documents_sha256", "sha256"),
    )


# Defined after the class so the DESC expression can reference the column.
Index("ix_documents_status_created_at", Document.status, Document.created_at.desc())


class DocumentUnit(Base):
    """Smallest parsed unit: a PDF page, an image/TIFF frame, or a sheet/CSV."""

    __tablename__ = "document_units"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    unit_type: Mapped[UnitType] = mapped_column(StrEnumText(UnitType))
    unit_index: Mapped[int] = mapped_column(Integer)
    sheet_name: Mapped[str | None]
    text: Mapped[str | None]
    extraction_method: Mapped[ExtractionMethod] = mapped_column(StrEnumText(ExtractionMethod))
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Confidence)
    structured_data: Mapped[dict[str, Any] | None]
    image_quality: Mapped[dict[str, Any] | None]
    # Set when this unit failed to parse; the rest of the document still proceeds.
    error_code: Mapped[ErrorCode | None] = mapped_column(StrEnumText(ErrorCode))
    # [{code, message, details?}] - non-fatal issues (truncation, repairs, ...)
    warnings: Mapped[list[Any]] = mapped_column(server_default=sql_text("'[]'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()

    document: Mapped[Document] = relationship(back_populates="units")

    __table_args__ = (
        # Leading document_id also serves as the FK index.
        UniqueConstraint("document_id", "unit_type", "unit_index"),
        # Target for chunks' composite FK (keeps chunks.document_id consistent).
        UniqueConstraint("id", "document_id"),
        CheckConstraint("unit_index >= 0", name="unit_index_nonneg"),
        confidence_check("ocr_confidence"),
        enum_check("unit_type", UnitType),
        enum_check("extraction_method", ExtractionMethod),
        enum_check("error_code", ErrorCode),
    )
