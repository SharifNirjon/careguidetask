"""The rule protocol and small helpers shared by all rules."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any, ClassVar, Final, Protocol

from app.models.enums import RuleCode, Severity
from app.validation.money import AmountCheck, compare_amounts, fmt
from app.validation.types import AMOUNT_FIELDS, LINE_AMOUNT_FIELDS, Issue, ValidationContext

FIELD_LABELS: Final[Mapping[str, str]] = {
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "supplier_name": "Supplier",
    "customer_name": "Customer",
    "subtotal": "Subtotal",
    "tax": "Tax",
    "total": "Total",
    "currency": "Currency",
    "description": "Description",
    "quantity": "Quantity",
    "unit_price": "Unit price",
    "amount": "Amount",
}
HEADER_FIELDS: Final = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                        "subtotal", "tax", "total", "currency")  # fmt: skip
REASON_UNGROUNDED: Final = frozenset({"ungrounded", "conflicting_rows"})
REASON_UNPARSEABLE: Final = "unparseable"


class ValidationRule(Protocol):
    """Pure: everything a rule needs is in the context; no DB, clock or network."""

    code: ClassVar[RuleCode]
    default_severity: ClassVar[Severity]

    def applies(self, ctx: ValidationContext) -> bool: ...

    def check(self, ctx: ValidationContext) -> list[Issue]: ...


def label(field_path: str) -> str:
    """'line_items[2].amount' -> 'Line 3 amount'; 'total' -> 'Total'."""
    if field_path.startswith("line_items["):
        index, _, name = field_path[len("line_items[") :].partition("].")
        return f"Line {int(index) + 1} {FIELD_LABELS.get(name, name).lower()}"
    return FIELD_LABELS.get(field_path, field_path)


def make_issue(
    rule: ValidationRule,
    field_path: str | None,
    message: str,
    *,
    severity: Severity | None = None,
    code: RuleCode | None = None,
    expected: str | None = None,
    actual: str | None = None,
    related: uuid.UUID | None = None,
) -> Issue:
    return Issue(
        rule_code=code or rule.code,
        severity=severity or rule.default_severity,
        field_path=field_path,
        message=message,
        expected_value=expected,
        actual_value=actual,
        related_extraction_id=related,
    )


def iter_field_metas(ctx: ValidationContext) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    """(field_path, field name, meta) for header fields and every line-item field."""
    extraction = ctx.extraction
    for name in HEADER_FIELDS:
        if meta := extraction.meta(name):
            yield name, name, meta
    for line in extraction.line_items:
        for name, meta in line.field_meta.items():
            if isinstance(meta, Mapping):
                yield f"line_items[{line.index}].{name}", name, meta


def is_amount_field(name: str) -> bool:
    return name in AMOUNT_FIELDS or name in LINE_AMOUNT_FIELDS


def check_amounts(ctx: ValidationContext, expected: Decimal, actual: Decimal, *, lines: int) -> AmountCheck:
    return compare_amounts(
        expected, actual, currency=ctx.extraction.currency, lines=lines, rel_tol=ctx.rel_tol
    )


def money(ctx: ValidationContext, amount: Decimal) -> str:
    return fmt(amount, ctx.extraction.currency)
