"""Internal result of extracting one invoice, before persistence (dataclasses only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from app.extraction.normalize import invoice_number_norm, supplier_name_norm
from app.llm import TokenUsage

Source = Literal["llm", "rule"]
FieldValue = str | Decimal | date | None


@dataclass(slots=True)
class FieldResult:
    value: FieldValue
    meta: dict[str, Any]  # one field_meta entry: confidence, evidence_text, unit_index, source, grounded, ...

    @property
    def confidence(self) -> float:
        return float(self.meta.get("confidence", 0.0))


@dataclass(slots=True)
class LineItemDraft:
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None
    meta: dict[str, Any]  # {"description": {...}, "quantity": {...}, ...}


@dataclass(slots=True)
class LLMTrace:
    model_name: str
    prompt_version: str
    raw_responses: list[dict[str, Any]]  # validated JSON, one per request part
    usage: TokenUsage
    latency_ms: int
    attempts: int
    cached_parts: int
    parts: int


@dataclass(slots=True)
class ExtractionDraft:
    unit_start: int
    unit_end: int
    document_type: str
    source: Source
    fields: dict[str, FieldResult]
    line_items: list[LineItemDraft] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    overall_confidence: float = 0.0
    trace: LLMTrace | None = None
    error: dict[str, Any] | None = None

    def value(self, name: str) -> FieldValue:
        result = self.fields.get(name)
        return result.value if result else None

    def column_values(self) -> dict[str, Any]:
        """Values for the extractions table columns (Step 2 schema)."""
        number = self.value("invoice_number")
        supplier = self.value("supplier_name")
        return {
            "invoice_number": number,
            "invoice_date": self.value("invoice_date"),
            "supplier_name": supplier,
            "customer_name": self.value("customer_name"),
            "subtotal": self.value("subtotal"),
            "tax": self.value("tax"),
            "total": self.value("total"),
            "currency": self.value("currency"),
            "invoice_number_norm": invoice_number_norm(number) if isinstance(number, str) else None,
            "supplier_name_norm": supplier_name_norm(supplier) if isinstance(supplier, str) else None,
        }

    def field_meta(self) -> dict[str, Any]:
        """Per-field provenance plus document-level notes (keys starting with '_')."""
        meta: dict[str, Any] = {name: result.meta for name, result in self.fields.items()}
        meta["_document_type"] = self.document_type
        meta["_source"] = self.source
        meta["_uncertainties"] = self.uncertainties
        if self.error is not None:
            meta["_error"] = self.error
        return meta
