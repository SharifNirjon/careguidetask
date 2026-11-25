"""Writes a document's chunks (no network: fast enough for the pipeline).

Chunks are rebuilt from scratch on every run, but an embedding is never thrown
away for nothing: a new chunk whose content_sha256 matches an already-embedded
old chunk of the same document inherits its vector, model, space and time.
Only genuinely new text lands in the pending queue (embedding IS NULL).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.config import SearchSettings
from app.models import Chunk, DocumentUnit, Extraction
from app.models.enums import ChunkKind
from app.models.search import content_sha256
from app.search.chunking import (
    ChunkDraft,
    ExtractionSummaryInput,
    LineSummary,
    UnitInput,
    chunk_unit,
    summary_chunk,
)

DOCUMENT_TYPE_KEY: Final = "_document_type"
EMBEDDING_COLUMNS: Final = ("embedding", "embedding_model", "embedding_space", "embedded_at")


@dataclass(frozen=True, slots=True)
class IndexReport:
    chunks: int
    reused_embeddings: int
    pending: int

    def to_details(self) -> dict[str, int]:
        return {"chunks": self.chunks, "reused_embeddings": self.reused_embeddings, "pending": self.pending}


def summary_input(extraction: Extraction) -> ExtractionSummaryInput:
    meta = extraction.field_meta or {}
    return ExtractionSummaryInput(
        document_type=str(meta.get(DOCUMENT_TYPE_KEY) or "invoice"),
        invoice_number=extraction.invoice_number,
        invoice_date=extraction.invoice_date,
        supplier_name=extraction.supplier_name,
        customer_name=extraction.customer_name,
        subtotal=extraction.subtotal,
        tax=extraction.tax,
        total=extraction.total,
        currency=extraction.currency.strip() if extraction.currency else None,
        line_items=[
            LineSummary(i.description, i.amount)
            for i in sorted(extraction.line_items, key=lambda li: li.position)
        ],
    )


def owning_extraction(unit_index: int, extractions: Sequence[Extraction]) -> uuid.UUID | None:
    """The one current extraction whose unit range contains the unit (None if 0 or 2+)."""
    owners = [e.id for e in extractions if e.unit_start <= unit_index <= e.unit_end]
    return owners[0] if len(owners) == 1 else None


class ChunkIndexer:
    def __init__(self, session: Session, settings: SearchSettings) -> None:
        self._session = session
        self._settings = settings

    def rebuild_document(self, document_id: uuid.UUID) -> IndexReport:
        """All content chunks + one summary per current extraction. Caller commits."""
        units = list(
            self._session.scalars(
                select(DocumentUnit)
                .where(DocumentUnit.document_id == document_id)
                .order_by(DocumentUnit.unit_index, DocumentUnit.unit_type)
            )
        )
        extractions = self._current_extractions(document_id)
        unit_ids = {u.unit_index: u.id for u in units}
        rows: list[tuple[ChunkDraft, uuid.UUID, uuid.UUID | None]] = []
        for unit in units:
            for draft in chunk_unit(
                self._unit_input(unit),
                max_tokens=self._settings.chunk_tokens,
                overlap_tokens=self._settings.chunk_overlap,
            ):
                rows.append((draft, unit.id, owning_extraction(unit.unit_index, extractions)))
        for extraction in extractions:
            unit_id = unit_ids.get(extraction.unit_start)
            if unit_id is not None:
                draft = summary_chunk(
                    summary_input(extraction), extraction.unit_start, max_tokens=self._settings.chunk_tokens
                )
                rows.append((draft, unit_id, extraction.id))
        reusable = self._embedded_by_sha(Chunk.document_id == document_id)
        self._session.execute(delete(Chunk).where(Chunk.document_id == document_id))
        return self._insert(document_id, rows, reusable, start_index=0)

    def rebuild_summary(self, extraction_id: uuid.UUID) -> IndexReport:
        """After a correction: replace this extraction's summary chunk only. Caller commits."""
        extraction = self._session.scalars(
            select(Extraction)
            .where(Extraction.id == extraction_id)
            .options(selectinload(Extraction.line_items))
        ).one()
        unit_id = self._session.scalar(
            select(DocumentUnit.id)
            .where(
                DocumentUnit.document_id == extraction.document_id,
                DocumentUnit.unit_index == extraction.unit_start,
            )
            .limit(1)
        )
        is_summary = (Chunk.extraction_id == extraction_id) & (
            Chunk.chunk_kind == ChunkKind.EXTRACTION_SUMMARY
        )
        reusable = self._embedded_by_sha(is_summary)
        self._session.execute(delete(Chunk).where(is_summary))
        if unit_id is None:
            return IndexReport(0, 0, 0)
        draft = summary_chunk(
            summary_input(extraction), extraction.unit_start, max_tokens=self._settings.chunk_tokens
        )
        next_index = (
            self._session.scalar(
                select(func.coalesce(func.max(Chunk.chunk_index) + 1, 0)).where(
                    Chunk.document_id == extraction.document_id
                )
            )
            or 0
        )
        return self._insert(
            extraction.document_id, [(draft, unit_id, extraction_id)], reusable, start_index=int(next_index)
        )

    # ------------------------------------------------------------ helpers

    def _current_extractions(self, document_id: uuid.UUID) -> list[Extraction]:
        return list(
            self._session.scalars(
                select(Extraction)
                .where(Extraction.document_id == document_id, Extraction.is_current.is_(True))
                .options(selectinload(Extraction.line_items))
                .order_by(Extraction.invoice_index)
            )
        )

    @staticmethod
    def _unit_input(unit: DocumentUnit) -> UnitInput:
        return UnitInput(
            unit_index=unit.unit_index,
            unit_type=unit.unit_type,
            text=unit.text,
            sheet_name=unit.sheet_name,
            structured_data=unit.structured_data,
        )

    def _embedded_by_sha(self, where: Any) -> dict[str, dict[str, Any]]:
        rows = self._session.execute(
            select(Chunk.content_sha256, *(getattr(Chunk, c) for c in EMBEDDING_COLUMNS)).where(
                where, Chunk.embedding.is_not(None)
            )
        ).all()
        return {row[0]: dict(zip(EMBEDDING_COLUMNS, row[1:], strict=True)) for row in rows}

    def _insert(
        self,
        document_id: uuid.UUID,
        rows: Sequence[tuple[ChunkDraft, uuid.UUID, uuid.UUID | None]],
        reusable: dict[str, dict[str, Any]],
        *,
        start_index: int,
    ) -> IndexReport:
        reused = 0
        for offset, (draft, unit_id, extraction_id) in enumerate(rows):
            sha = content_sha256(draft.content)
            vector = reusable.get(sha, {})
            reused += bool(vector)
            self._session.add(
                Chunk(
                    document_id=document_id,
                    unit_id=unit_id,
                    extraction_id=extraction_id,
                    chunk_index=start_index + offset,
                    chunk_kind=draft.kind,
                    content=draft.content,
                    content_sha256=sha,
                    token_count=draft.token_count,
                    **vector,
                )
            )
        self._session.flush()
        return IndexReport(chunks=len(rows), reused_embeddings=reused, pending=len(rows) - reused)
