"""Builders for InvoiceExtractionLLM test data."""

from __future__ import annotations

from typing import Any

from app.extraction.inputs import UnitText
from app.extraction.schemas import (
    HEADER_FIELDS,
    ExtractedField,
    InvoiceExtractionLLM,
    LineItemLLM,
    empty_field,
)
from app.models.enums import ExtractionMethod, UnitType

FieldSpec = tuple[str, str, int] | tuple[str, str, int, float]


def field(spec: FieldSpec | None) -> ExtractedField:
    if spec is None:
        return empty_field()
    raw, evidence, unit_index, *rest = spec
    return ExtractedField(
        raw_value=raw, evidence=evidence, unit_index=unit_index, confidence=rest[0] if rest else 0.95
    )


def llm_result(
    *,
    document_type: str = "invoice",
    line_items: list[dict[str, FieldSpec | None]] | None = None,
    uncertainties: list[str] | None = None,
    **headers: FieldSpec | None,
) -> InvoiceExtractionLLM:
    values: dict[str, Any] = {name: field(headers.get(name)) for name in HEADER_FIELDS}
    items = [
        LineItemLLM(**{k: field(item.get(k)) for k in ("description", "quantity", "unit_price", "amount")})
        for item in line_items or []
    ]
    return InvoiceExtractionLLM(
        document_type=document_type,
        line_items=items,  # type: ignore[arg-type]
        uncertainties=uncertainties or [],
        **values,
    )


def page(index: int, text: str, ocr: float | None = None) -> UnitText:
    return UnitText(
        unit_index=index,
        unit_type=UnitType.PDF_PAGE,
        text=text,
        extraction_method=ExtractionMethod.OCR if ocr is not None else ExtractionMethod.DIGITAL,
        ocr_confidence=ocr,
    )


INVOICE_PAGE = """ACME Supplies Ltd
INVOICE
Invoice Number: INV-2024-001
Date: 05/03/2024   Due: 25/03/2024
Bill To: Northwind Clinic
Nitrile gloves (box)  10  12.50  125.00
Subtotal 225.00
VAT 20% 45.00
Total Amount Due 270.00 EUR"""
