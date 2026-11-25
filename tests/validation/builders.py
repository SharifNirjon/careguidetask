"""Snapshot/context builders. The defaults are a clean, consistent invoice."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.config import ValidationSettings
from app.validation.types import (
    Candidate,
    DocumentInfo,
    ExtractionSnapshot,
    LineSnapshot,
    MatchKind,
    SourceKind,
    ValidationContext,
)

D = Decimal
TODAY = date(2024, 4, 1)
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


def meta(confidence: float = 0.95, **extra: Any) -> dict[str, Any]:
    return {"confidence": confidence, "grounded": True, "source": "llm", "evidence_text": "x", **extra}


def line(
    index: int, qty: str | None, price: str | None, amount: str | None, desc: str = "item"
) -> LineSnapshot:
    return LineSnapshot(
        index=index,
        description=desc,
        quantity=D(qty) if qty else None,
        unit_price=D(price) if price else None,
        amount=D(amount) if amount else None,
    )


def snapshot(**overrides: Any) -> ExtractionSnapshot:
    base = ExtractionSnapshot(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version=1,
        document_type="invoice",
        invoice_number="INV-2024-001",
        invoice_date=date(2024, 3, 5),
        supplier_name="ACME Supplies Ltd",
        customer_name="Northwind Clinic",
        subtotal=D("225.00"),
        tax=D("45.00"),
        total=D("270.00"),
        currency="EUR",
        invoice_number_norm="INV2024001",
        supplier_name_norm="acme supplies",
        overall_confidence=0.95,
        field_meta={name: meta() for name in HEADER},
        line_items=(line(0, "10", "12.50", "125.00"), line(1, "5", "20.00", "100.00")),
    )
    return replace(base, **overrides)


def document(
    kind: SourceKind = SourceKind.DOCUMENT,
    *,
    created: int = 0,
    sha: str | None = None,
    name: str = "invoice.pdf",
) -> DocumentInfo:
    """Each document gets its own content hash unless `sha` says they're identical files."""
    return DocumentInfo(
        id=uuid.uuid4(),
        source_kind=kind,
        created_at=datetime(2024, 3, 10, 12, 0, created, tzinfo=UTC),
        sha256=sha or uuid.uuid4().hex * 2,
        filename=name,
    )


def candidate(
    extraction: ExtractionSnapshot,
    doc: DocumentInfo,
    kind: MatchKind = MatchKind.EXACT,
    similarity: float = 1.0,
) -> Candidate:
    return Candidate(extraction=extraction, document=doc, supplier_similarity=similarity, kind=kind)


def context(
    extraction: ExtractionSnapshot | None = None,
    *,
    doc: DocumentInfo | None = None,
    candidates: tuple[Candidate, ...] = (),
    today: date = TODAY,
    settings: ValidationSettings | None = None,
) -> ValidationContext:
    return ValidationContext(
        extraction=extraction or snapshot(),
        document=doc or document(),
        today=today,
        settings=settings or ValidationSettings(),
        candidates=candidates,
    )


def with_meta(extraction: ExtractionSnapshot, name: str, **values: Any) -> ExtractionSnapshot:
    field_meta = dict(extraction.field_meta)
    field_meta[name] = {**field_meta.get(name, {}), **values}
    return replace(extraction, field_meta=field_meta)
