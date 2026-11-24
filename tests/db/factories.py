"""Small factories for DB tests (flush, don't commit)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import Document, DocumentUnit, Extraction, ExtractionMethod, UnitType



def make_document(session: Session, **kw: Any) -> Document:
    token = uuid.uuid4().hex
    defaults: dict[str, Any] = dict(
        original_filename=f"{token}.pdf",
        storage_key=f"test/{token}.pdf",
        mime_type="application/pdf",
        file_ext="pdf",
        size_bytes=1024,
        sha256=(token * 2)[:64],
    )
    doc = Document(**{**defaults, **kw})
    session.add(doc)
    session.flush()
    return doc


def make_unit(session: Session, document: Document, index: int = 0, **kw: Any) -> DocumentUnit:
    defaults: dict[str, Any] = dict(
        unit_type=UnitType.PDF_PAGE,
        unit_index=index,
        text=f"page {index}",
        extraction_method=ExtractionMethod.DIGITAL,
    )
    unit = DocumentUnit(document_id=document.id, **{**defaults, **kw})
    session.add(unit)
    session.flush()
    return unit


def make_extraction(session: Session, document: Document, **kw: Any) -> Extraction:
    defaults: dict[str, Any] = dict(invoice_index=0, unit_start=0, unit_end=0)
    ext = Extraction(document_id=document.id, **{**defaults, **kw})
    session.add(ext)
    session.flush()
    return ext
