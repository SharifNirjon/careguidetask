"""Numeric validity: are the amounts plausible numbers at all?"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from app.models.enums import RuleCode, Severity
from app.validation.rules.base import (
    REASON_UNPARSEABLE,
    is_amount_field,
    iter_field_metas,
    label,
    make_issue,
    money,
)
from app.validation.types import AMOUNT_FIELDS, Issue, ValidationContext

CREDIT_NOTE = "credit_note"
ZERO = Decimal(0)


class InvalidAmount:
    """A raw amount was printed but could not be read as a number."""

    code: ClassVar = RuleCode.INVALID_AMOUNT
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        return True

    def check(self, ctx: ValidationContext) -> list[Issue]:
        return [
            make_issue(
                self,
                path,
                f"{label(path)} {meta['rejected_value']!r} is not a valid amount.",
                actual=str(meta["rejected_value"]),
            )
            for path, name, meta in iter_field_metas(ctx)
            if is_amount_field(name) and meta.get("reason") == REASON_UNPARSEABLE
        ]


class NegativeAmount:
    code: ClassVar = RuleCode.NEGATIVE_AMOUNT
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.document_type != CREDIT_NOTE

    def check(self, ctx: ValidationContext) -> list[Issue]:
        issues = []
        for name in ("subtotal", "total"):
            value = ctx.extraction.value(name)
            if value is not None and value < ZERO:
                issues.append(
                    make_issue(
                        self,
                        name,
                        f"{label(name)} {money(ctx, value)} is negative on a "
                        f"{ctx.extraction.document_type.replace('_', ' ')} "
                        "(only credit notes may be negative).",
                        actual=money(ctx, value),
                    )
                )
        return issues


class SignInconsistent:
    code: ClassVar = RuleCode.SIGN_INCONSISTENT
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.document_type == CREDIT_NOTE

    def check(self, ctx: ValidationContext) -> list[Issue]:
        values = {n: ctx.extraction.value(n) for n in AMOUNT_FIELDS}
        signs = {n: v > ZERO for n, v in values.items() if v is not None and v != ZERO}
        if len(set(signs.values())) <= 1:
            return []
        shown = ", ".join(f"{n} {money(ctx, values[n])}" for n in signs)
        return [make_issue(self, None, f"Credit note mixes positive and negative amounts: {shown}.")]


class ImplausibleTaxRate:
    code: ClassVar = RuleCode.IMPLAUSIBLE_TAX_RATE
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        e = ctx.extraction
        return e.tax is not None and e.subtotal is not None and e.subtotal != ZERO

    def check(self, ctx: ValidationContext) -> list[Issue]:
        e = ctx.extraction
        if e.tax is None or e.subtotal is None or e.subtotal == ZERO:
            return []
        rate = e.tax / e.subtotal
        maximum = Decimal(str(ctx.settings.max_tax_rate))
        if ZERO <= rate <= maximum:
            return []
        return [
            make_issue(
                self,
                "tax",
                f"Tax {money(ctx, e.tax)} is {rate:.1%} of subtotal {money(ctx, e.subtotal)}, "
                f"outside 0%-{maximum:.0%}.",
                actual=f"{rate:.4f}",
                expected=f"0-{maximum}",
            )
        ]


class ZeroTotal:
    code: ClassVar = RuleCode.ZERO_TOTAL
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.total is not None

    def check(self, ctx: ValidationContext) -> list[Issue]:
        if ctx.extraction.total == ZERO:
            return [make_issue(self, "total", "Total is zero.", actual=money(ctx, ZERO))]
        return []
