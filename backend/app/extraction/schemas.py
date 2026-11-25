"""What the LLM must return: RAW strings copied verbatim, plus evidence.

The model never converts formats or computes anything; normalize.py does.
Every field is required in the JSON (strict mode); absent values are null.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DocumentType = Literal["invoice", "receipt", "credit_note", "statement", "other"]
EVIDENCE_MAX_CHARS: Final = 200

HEADER_FIELDS: Final = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "customer_name",
    "subtotal",
    "tax",
    "total",
    "currency",
)
LINE_ITEM_FIELDS: Final = ("description", "quantity", "unit_price", "amount")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedField(_Strict):
    raw_value: str | None = Field(description="Exactly as printed in the document, or null.")
    evidence: str | None = Field(
        description="Exact substring of the document containing raw_value (max 200 chars)."
    )
    unit_index: int | None = Field(description="Number of the UNIT the evidence comes from.")
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence")
    @classmethod
    def _clip_evidence(cls, value: str | None) -> str | None:
        """A prefix of an exact substring is still an exact substring: trim, don't reject."""
        return value[:EVIDENCE_MAX_CHARS] if value is not None else None

    @property
    def is_empty(self) -> bool:
        return self.raw_value is None or not self.raw_value.strip()


class LineItemLLM(_Strict):
    description: ExtractedField
    quantity: ExtractedField
    unit_price: ExtractedField
    amount: ExtractedField


class InvoiceExtractionLLM(_Strict):
    document_type: DocumentType
    invoice_number: ExtractedField
    invoice_date: ExtractedField
    supplier_name: ExtractedField
    customer_name: ExtractedField
    subtotal: ExtractedField
    tax: ExtractedField
    total: ExtractedField
    currency: ExtractedField
    line_items: list[LineItemLLM]
    uncertainties: list[str]

    def header(self, name: str) -> ExtractedField:
        if name not in HEADER_FIELDS:
            raise KeyError(name)
        field: ExtractedField = getattr(self, name)
        return field


def empty_field() -> ExtractedField:
    return ExtractedField(raw_value=None, evidence=None, unit_index=None, confidence=0.0)
