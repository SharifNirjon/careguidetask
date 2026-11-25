"""Immutable inputs and outputs of the rules (rules never see the ORM or the DB)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from app.config import ValidationSettings
from app.models import issue_fingerprint
from app.models.enums import RuleCode, Severity

AMOUNT_FIELDS: Final = ("subtotal", "tax", "total")
LINE_AMOUNT_FIELDS: Final = ("quantity", "unit_price", "amount")


class SourceKind(StrEnum):
    """Scanned/born-digital documents vs ledgers: decides duplicate vs cross-check."""

    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"


class MatchKind(StrEnum):
    EXACT = "exact"  # same invoice number, similar supplier
    FUZZY = "fuzzy"  # similar supplier, same total, close date, different/missing number


@dataclass(frozen=True, slots=True)
class LineSnapshot:
    index: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None
    field_meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtractionSnapshot:
    id: uuid.UUID
    document_id: uuid.UUID
    version: int
    document_type: str
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    customer_name: str | None
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    currency: str | None
    invoice_number_norm: str | None
    supplier_name_norm: str | None
    overall_confidence: float | None
    field_meta: Mapping[str, Any] = field(default_factory=dict)
    line_items: tuple[LineSnapshot, ...] = ()

    def meta(self, name: str) -> Mapping[str, Any]:
        value = self.field_meta.get(name)
        return value if isinstance(value, Mapping) else {}

    def value(self, name: str) -> Any:
        return getattr(self, name)

    @property
    def line_count(self) -> int:
        return len(self.line_items)

    @property
    def line_sum(self) -> Decimal | None:
        """Σ line amounts, or None when any line lacks an amount (incomplete sum)."""
        amounts = [line.amount for line in self.line_items]
        if not amounts or any(a is None for a in amounts):
            return None
        return sum((a for a in amounts if a is not None), Decimal(0))


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    id: uuid.UUID
    source_kind: SourceKind
    created_at: datetime
    sha256: str
    filename: str

    def is_later_than(self, other: DocumentInfo) -> bool:
        """'Later' = uploaded later; ties broken by id so exactly one side is later."""
        return (self.created_at, self.id) > (other.created_at, other.id)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Another document's current extraction that may be the same invoice."""

    extraction: ExtractionSnapshot
    document: DocumentInfo
    supplier_similarity: float
    kind: MatchKind


@dataclass(frozen=True, slots=True)
class ValidationContext:
    extraction: ExtractionSnapshot
    document: DocumentInfo
    today: date
    settings: ValidationSettings
    candidates: tuple[Candidate, ...] = ()

    @property
    def rel_tol(self) -> Decimal:
        return Decimal(str(self.settings.validation_rel_tol))


@dataclass(frozen=True, slots=True)
class Issue:
    rule_code: RuleCode
    severity: Severity
    field_path: str | None
    message: str
    expected_value: str | None = None
    actual_value: str | None = None
    related_extraction_id: uuid.UUID | None = None

    @property
    def fingerprint(self) -> str:
        return issue_fingerprint(self.rule_code.value, self.field_path, self.related_extraction_id)
