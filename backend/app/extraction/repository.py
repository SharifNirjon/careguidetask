"""DB edge for extraction: load a document's units/segments, persist drafts.

Persistence reuses app.db.versioning.add_extraction_version (Step 2): it locks
the document row, flips the previous is_current and inserts the new version
in the caller's transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.versioning import add_extraction_version
from app.extraction.draft import ExtractionDraft
from app.extraction.inputs import UnitText
from app.extraction.tabular import TableUnit
from app.models import Document, DocumentUnit, Extraction, LineItem
from app.models.enums import UnitType

CONFIDENCE_QUANT: Final = Decimal("0.001")
TABULAR_UNIT_TYPES: Final = frozenset({UnitType.SHEET, UnitType.CSV})


@dataclass(frozen=True, slots=True)
class StoredUnit:
    text: UnitText
    structured_data: dict[str, Any] | None

    @property
    def is_tabular(self) -> bool:
        return self.text.unit_type in TABULAR_UNIT_TYPES

    def as_table(self) -> TableUnit:
        return TableUnit(
            unit_index=self.text.unit_index,
            sheet_name=self.text.sheet_name,
            structured_data=self.structured_data or {},
            text=self.text.text,
        )


@dataclass(frozen=True, slots=True)
class DocumentInputs:
    document_id: uuid.UUID
    units: list[StoredUnit]
    segments: list[tuple[int, int]]  # (unit_start, unit_end) inclusive


class ExtractionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def load_inputs(self, document_id: uuid.UUID) -> DocumentInputs:
        doc = self._session.get_one(Document, document_id)
        rows = self._session.scalars(
            select(DocumentUnit)
            .where(DocumentUnit.document_id == document_id)
            .order_by(DocumentUnit.unit_type, DocumentUnit.unit_index)
        ).all()
        units = [
            StoredUnit(
                text=UnitText(
                    unit_index=row.unit_index,
                    unit_type=row.unit_type,
                    text=row.text or "",
                    extraction_method=row.extraction_method,
                    ocr_confidence=float(row.ocr_confidence) if row.ocr_confidence is not None else None,
                    sheet_name=row.sheet_name,
                ),
                structured_data=row.structured_data,
            )
            for row in rows
        ]
        segments = [(int(s["unit_start"]), int(s["unit_end"])) for s in doc.segments or []]
        if not segments and units:
            segments = [(units[0].text.unit_index, units[-1].text.unit_index)]
        return DocumentInputs(document_id, units, segments)

    def indexes_done_by_job(self, document_id: uuid.UUID, job_id: uuid.UUID) -> set[int]:
        """invoice_index values this job already persisted (a Celery retry skips them)."""
        return set(
            self._session.scalars(
                select(Extraction.invoice_index).where(
                    Extraction.document_id == document_id, Extraction.processing_job_id == job_id
                )
            )
        )

    def save(
        self,
        document_id: uuid.UUID,
        job_id: uuid.UUID,
        invoice_index: int,
        draft: ExtractionDraft,
        *,
        extractor_version: str,
    ) -> uuid.UUID:
        """Insert a new current version + its line items (one transaction per call)."""
        existing = self._session.scalar(
            select(Extraction.id).where(
                Extraction.document_id == document_id,
                Extraction.invoice_index == invoice_index,
                Extraction.processing_job_id == job_id,
            )
        )
        if existing is not None:
            return existing  # idempotent under retry, even if the pre-check raced
        trace = draft.trace
        extraction = add_extraction_version(
            self._session,
            document_id=document_id,
            invoice_index=invoice_index,
            unit_start=draft.unit_start,
            unit_end=draft.unit_end,
            field_meta=draft.field_meta(),
            overall_confidence=Decimal(str(draft.overall_confidence)).quantize(CONFIDENCE_QUANT),
            model_name=trace.model_name if trace else f"rule:{extractor_version}",
            prompt_version=trace.prompt_version if trace else extractor_version,
            raw_llm_response={"parts": trace.raw_responses} if trace else None,
            token_usage={
                **trace.usage.to_dict(),
                "requests": trace.attempts,
                "cached_parts": trace.cached_parts,
            }
            if trace
            else None,
            latency_ms=trace.latency_ms if trace else None,
            processing_job_id=job_id,
            **draft.column_values(),
        )
        for position, item in enumerate(draft.line_items):
            self._session.add(
                LineItem(
                    extraction_id=extraction.id,
                    position=position,
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    amount=item.amount,
                    field_meta=item.meta,
                )
            )
        self._session.flush()
        return extraction.id
