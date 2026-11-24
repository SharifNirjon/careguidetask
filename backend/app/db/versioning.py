"""Versioned extractions: insert a new version and flip is_current atomically."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Document, Extraction


def add_extraction_version(
    session: Session, *, document_id: uuid.UUID, invoice_index: int, **fields: Any
) -> Extraction:
    """Add a new current version of (document_id, invoice_index).

    Runs inside the caller's transaction, so the flip and the insert commit (or
    roll back) together.

    Order matters: the partial unique index on (document_id, invoice_index)
    WHERE is_current cannot be deferred, so the previous version is set
    is_current = false and FLUSHED before the new row is inserted.

    Concurrency: the parent document row is locked FOR NO KEY UPDATE, so two
    reprocessing runs for the same document serialize instead of racing on
    the version number. (NO KEY UPDATE does not block FK inserts into child
    tables.) Flipping via the ORM, not a bulk UPDATE, bumps lock_version, so
    a reviewer still editing the old version gets a StaleDataError.
    """
    session.execute(
        select(Document.id).where(Document.id == document_id).with_for_update(key_share=True)
    ).one()  # raises NoResultFound for an unknown document

    current = session.scalars(
        select(Extraction).where(
            Extraction.document_id == document_id,
            Extraction.invoice_index == invoice_index,
            Extraction.is_current.is_(True),
        )
    ).one_or_none()
    max_version = session.scalar(
        select(func.max(Extraction.version)).where(
            Extraction.document_id == document_id,
            Extraction.invoice_index == invoice_index,
        )
    )

    if current is not None:
        current.is_current = False
        session.flush()

    new = Extraction(
        document_id=document_id,
        invoice_index=invoice_index,
        version=(max_version or 0) + 1,
        is_current=True,
        **fields,
    )
    session.add(new)
    session.flush()
    return new
