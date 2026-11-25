"""Committed test data for DB integration tests: documents + current extractions."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.db.versioning import add_extraction_version
from app.extraction.normalize import invoice_number_norm, supplier_name_norm
from app.models import Document, DocumentStatus, LineItem

PDF = "application/pdf"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BASE_TIME = datetime(2024, 3, 10, 12, 0, tzinfo=UTC)
HEADER = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "customer_name",
    "subtotal",
    "tax",
    "total",
    "currency",
)


def add_document(
    session: Session, *, name: str = "invoice.pdf", mime: str = PDF, sha: str | None = None, minutes: int = 0
) -> Document:
    doc = Document(
        original_filename=name,
        storage_key=f"test/{uuid.uuid4().hex}",
        mime_type=mime,
        file_ext=name.rsplit(".", maxsplit=1)[-1],
        size_bytes=100,
        sha256=sha or uuid.uuid4().hex * 2,
        status=DocumentStatus.COMPLETED,
        created_at=BASE_TIME + timedelta(minutes=minutes),
    )
    session.add(doc)
    session.flush()
    return doc


def add_invoice(
    session: Session,
    document: Document,
    *,
    invoice_index: int = 0,
    number: str | None = "INV-2024-001",
    supplier: str | None = "ACME Supplies Ltd",
    customer: str | None = "Northwind Clinic",
    issued: date | None = date(2024, 3, 5),
    subtotal: str | None = "225.00",
    tax: str | None = "45.00",
    total: str | None = "270.00",
    currency: str | None = "EUR",
    lines: list[tuple[str, str, str]] | None = None,
    confidence: float = 0.95,
    document_type: str = "invoice",
) -> uuid.UUID:
    meta: dict[str, Any] = {n: {"confidence": confidence, "grounded": True, "source": "llm"} for n in HEADER}
    meta["_document_type"] = document_type
    extraction = add_extraction_version(
        session,
        document_id=document.id,
        invoice_index=invoice_index,
        unit_start=0,
        unit_end=0,
        invoice_number=number,
        invoice_number_norm=invoice_number_norm(number),
        supplier_name=supplier,
        supplier_name_norm=supplier_name_norm(supplier),
        customer_name=customer,
        invoice_date=issued,
        subtotal=Decimal(subtotal) if subtotal else None,
        tax=Decimal(tax) if tax else None,
        total=Decimal(total) if total else None,
        currency=currency,
        field_meta=meta,
        overall_confidence=Decimal(str(confidence)),
    )
    for position, (qty, price, amount) in enumerate(
        lines if lines is not None else [("10", "12.50", "125.00"), ("5", "20.00", "100.00")]
    ):
        session.add(
            LineItem(
                extraction_id=extraction.id,
                position=position,
                quantity=Decimal(qty),
                unit_price=Decimal(price),
                amount=Decimal(amount),
            )
        )
    session.flush()
    return extraction.id
