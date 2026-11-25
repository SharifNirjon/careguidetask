"""Factories for API tests: committed documents + extractions (+ issues, + search chunks)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.api.container import Container
from app.models import DocumentUnit
from app.models.enums import ExtractionMethod, UnitType
from app.search.indexer import ChunkIndexer
from tests.validation.dbseed import add_document, add_invoice


def seeded_invoice(
    session: Session,
    container: Container,
    *,
    validate: bool = True,
    index: bool = True,
    name: str = "invoice.pdf",
    minutes: int = 0,
    page_text: str | None = None,
    **invoice: Any,
) -> tuple[uuid.UUID, uuid.UUID]:
    """(document_id, extraction_id), committed. Validated (so issues exist) and
    indexed for search (one page unit + its chunks), like a processed upload."""
    document = add_document(session, name=name, minutes=minutes)
    session.add(
        DocumentUnit(
            document_id=document.id,
            unit_type=UnitType.PDF_PAGE,
            unit_index=0,
            extraction_method=ExtractionMethod.DIGITAL,
            text=page_text or f"Invoice {invoice.get('number', 'INV-2024-001')}",
        )
    )
    extraction_id = add_invoice(session, document, **invoice)
    session.commit()
    if validate:
        container.validation_service().validate_document(document.id)
    if index:
        index_document(session, container, document.id)
    return document.id, extraction_id


def index_document(session: Session, container: Container, document_id: uuid.UUID) -> None:
    session.expire_all()
    ChunkIndexer(session, container.search_settings).rebuild_document(document_id)
    session.commit()


def mismatched_total(session: Session, container: Container, **kw: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """225 + 45 != 300: TOTAL_MISMATCH (an error), so the invoice needs review."""
    return seeded_invoice(session, container, total="300.00", **kw)
