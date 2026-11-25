"""Factories for API tests: committed documents + extractions (+ validation issues)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.api.container import Container
from tests.validation.dbseed import add_document, add_invoice


def seeded_invoice(
    session: Session,
    container: Container,
    *,
    validate: bool = True,
    name: str = "invoice.pdf",
    minutes: int = 0,
    **invoice: Any,
) -> tuple[uuid.UUID, uuid.UUID]:
    """(document_id, extraction_id), committed; validated so issues exist."""
    document = add_document(session, name=name, minutes=minutes)
    extraction_id = add_invoice(session, document, **invoice)
    session.commit()
    if validate:
        container.validation_service().validate_document(document.id)
    return document.id, extraction_id


def mismatched_total(session: Session, container: Container, **kw: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """225 + 45 != 300: TOTAL_MISMATCH (an error), so the invoice needs review."""
    return seeded_invoice(session, container, total="300.00", **kw)
