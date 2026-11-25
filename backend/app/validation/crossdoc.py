"""Duplicate vs cross-document classification. Pure and table-tested.

| source kinds          | key fields equal? | result for THIS extraction                 |
|-----------------------|-------------------|--------------------------------------------|
| same (doc<->doc)      | yes               | DUPLICATE_INVOICE if this is the LATER one |
| same (doc<->doc)      | no                | INVOICE_NUMBER_CONFLICT                     |
| different (doc<->sheet)| yes              | CROSS_DOC_VERIFIED (info)                  |
| different (doc<->sheet)| no               | CROSS_DOC_MISMATCH, differences listed     |

Fuzzy matches (no shared invoice number) only ever give POSSIBLE_DUPLICATE,
and only within the same source kind: a ledger row that merely resembles a
PDF is not evidence of a duplicate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Final

from app.extraction.normalize import supplier_name_norm
from app.models.enums import ReconciliationStatus, RuleCode
from app.validation.money import compare_amounts, fmt
from app.validation.types import ExtractionSnapshot, MatchKind, SourceKind

_TRIGRAM_WORD: Final = re.compile(r"[^\W_]+")


@dataclass(frozen=True, slots=True)
class FieldDiff:
    field: str
    left: str | None
    right: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {"field": self.field, "left": self.left, "right": self.right}


@dataclass(frozen=True, slots=True)
class KeyComparison:
    differences: tuple[FieldDiff, ...]
    not_compared: tuple[str, ...]  # present on only one side: not evidence either way

    @property
    def equal(self) -> bool:
        return not self.differences

    @property
    def status(self) -> ReconciliationStatus:
        return ReconciliationStatus.MATCHED if self.equal else ReconciliationStatus.MISMATCHED


def _compare_amount(
    name: str, a: Decimal, b: Decimal, currency: str | None, lines: int, rel_tol: Decimal
) -> FieldDiff | None:
    if compare_amounts(a, b, currency=currency, lines=lines, rel_tol=rel_tol).mismatch:
        return FieldDiff(name, fmt(a, currency), fmt(b, currency))
    return None


def compare_key_fields(
    left: ExtractionSnapshot, right: ExtractionSnapshot, *, rel_tol: Decimal
) -> KeyComparison:
    """Compare what both sides actually printed. A field missing on one side is
    'not compared' (ledger exports often lack tax or line items) — not a diff."""
    diffs: list[FieldDiff] = []
    skipped: list[str] = []
    currency = left.currency or right.currency
    lines = max(left.line_count, right.line_count)

    for name in ("total", "subtotal", "tax"):
        a, b = left.value(name), right.value(name)
        if a is None or b is None:
            skipped.append(name)
        elif diff := _compare_amount(name, a, b, currency, lines, rel_tol):
            diffs.append(diff)

    for name, a, b in (
        ("currency", left.currency, right.currency),
        ("invoice_date", left.invoice_date, right.invoice_date),
        ("customer_name", supplier_name_norm(left.customer_name), supplier_name_norm(right.customer_name)),
    ):
        if a is None or b is None:
            skipped.append(name)
        elif a != b:
            diffs.append(FieldDiff(name, str(a), str(b)))

    if left.line_items and right.line_items:
        if left.line_count != right.line_count:
            diffs.append(FieldDiff("line_count", str(left.line_count), str(right.line_count)))
        left_sum, right_sum = left.line_sum, right.line_sum
        if (
            left_sum is not None
            and right_sum is not None
            and (diff := _compare_amount("line_sum", left_sum, right_sum, currency, lines, rel_tol))
        ):
            diffs.append(diff)
    else:
        skipped.extend(["line_count", "line_sum"])
    return KeyComparison(tuple(diffs), tuple(skipped))


def classify(
    kind: MatchKind,
    this_kind: SourceKind,
    other_kind: SourceKind,
    comparison: KeyComparison,
    *,
    this_is_later: bool,
) -> RuleCode | None:
    """The issue THIS extraction gets for one candidate (None = nothing to report)."""
    same_kind = this_kind is other_kind
    if kind is MatchKind.FUZZY:
        return RuleCode.POSSIBLE_DUPLICATE if same_kind and this_is_later else None
    if same_kind:
        if comparison.equal:
            return RuleCode.DUPLICATE_INVOICE if this_is_later else None
        return RuleCode.INVOICE_NUMBER_CONFLICT
    return RuleCode.CROSS_DOC_VERIFIED if comparison.equal else RuleCode.CROSS_DOC_MISMATCH


def within_days(a: ExtractionSnapshot, b: ExtractionSnapshot, days: int) -> bool:
    if a.invoice_date is None or b.invoice_date is None:
        return False
    return abs(a.invoice_date - b.invoice_date) <= timedelta(days=days)


def trigrams(text: str) -> set[str]:
    """pg_trgm's trigram set: lower-cased words, each padded '  word '."""
    grams: set[str] = set()
    for word in _TRIGRAM_WORD.findall(text.lower()):
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return grams


def trigram_similarity(a: str | None, b: str | None) -> float:
    """Mirror of pg_trgm similarity(), so batch matching agrees with the SQL finder."""
    if not a or not b:
        return 0.0
    left, right = trigrams(a), trigrams(b)
    union = left | right
    return len(left & right) / len(union) if union else 0.0
