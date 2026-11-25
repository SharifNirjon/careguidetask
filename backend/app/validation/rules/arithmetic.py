"""Arithmetic consistency. Messages show the math; expected_value carries the
computed number so the UI can offer it as a suggestion (never written back)."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from app.models.enums import RuleCode, Severity
from app.validation.rules.base import check_amounts, make_issue, money
from app.validation.types import Issue, ValidationContext

ZERO = Decimal(0)


class LineAmountMismatch:
    code: ClassVar = RuleCode.LINE_AMOUNT_MISMATCH
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        return bool(ctx.extraction.line_items)

    def check(self, ctx: ValidationContext) -> list[Issue]:
        issues = []
        for line in ctx.extraction.line_items:
            if line.quantity is None or line.unit_price is None or line.amount is None:
                continue
            expected = line.quantity * line.unit_price
            result = check_amounts(ctx, expected, line.amount, lines=0)
            if result.mismatch:
                issues.append(
                    make_issue(
                        self,
                        f"line_items[{line.index}].amount",
                        f"Line {line.index + 1}: {line.quantity:f} x {money(ctx, line.unit_price)} = "
                        f"{money(ctx, expected)}, but the amount is {money(ctx, line.amount)} "
                        f"(difference {money(ctx, result.difference)}).",
                        expected=money(ctx, expected),
                        actual=money(ctx, line.amount),
                    )
                )
        return issues


class LineSumMismatch:
    """Σ line amounts vs subtotal, recognizing tax-inclusive line prices.

    Emits TAX_INCLUSIVE_LINES (info) instead of an error when the lines add up
    to the total and subtotal + tax does too: lines were printed gross.
    """

    code: ClassVar = RuleCode.LINE_SUM_MISMATCH
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        e = ctx.extraction
        return e.line_sum is not None and (e.subtotal is not None or e.total is not None)

    def check(self, ctx: ValidationContext) -> list[Issue]:
        e = ctx.extraction
        line_sum = e.line_sum
        if line_sum is None:
            return []
        lines = e.line_count
        if e.subtotal is not None:
            base, base_label = e.subtotal, f"subtotal {money(ctx, e.subtotal)}"
        else:
            if e.total is None:
                return []
            tax = e.tax or ZERO
            base = e.total - tax
            base_label = (
                (
                    f"total {money(ctx, e.total)} - tax {money(ctx, tax)} = {money(ctx, base)} "
                    f"(no subtotal printed)"
                )
                if e.tax is not None
                else (f"total {money(ctx, e.total)} (no subtotal or tax printed)")
            )
        result = check_amounts(ctx, line_sum, base, lines=lines)
        if not result.mismatch:
            return []
        if self._tax_inclusive(ctx, line_sum, lines):
            return [
                make_issue(
                    self,
                    "line_items",
                    f"Line amounts include tax: they sum to the total "
                    f"{money(ctx, line_sum)}, and subtotal + tax matches it.",
                    code=RuleCode.TAX_INCLUSIVE_LINES,
                    severity=Severity.INFO,
                )
            ]
        return [
            make_issue(
                self,
                "subtotal" if e.subtotal is not None else "total",
                f"Line amounts sum to {money(ctx, line_sum)} over {lines} lines, but {base_label} "
                f"(difference {money(ctx, result.difference)}).",
                expected=money(ctx, line_sum),
                actual=money(ctx, base),
            )
        ]

    @staticmethod
    def _tax_inclusive(ctx: ValidationContext, line_sum: Decimal, lines: int) -> bool:
        e = ctx.extraction
        if e.total is None or e.tax is None or e.tax == ZERO:
            return False
        lines_match_total = not check_amounts(ctx, line_sum, e.total, lines=lines).mismatch
        if e.subtotal is None:
            return lines_match_total
        return lines_match_total and not check_amounts(ctx, e.subtotal + e.tax, e.total, lines=lines).mismatch


class TotalMismatch:
    code: ClassVar = RuleCode.TOTAL_MISMATCH
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        e = ctx.extraction
        return e.subtotal is not None and e.tax is not None and e.total is not None

    def check(self, ctx: ValidationContext) -> list[Issue]:
        e = ctx.extraction
        if e.subtotal is None or e.tax is None or e.total is None:
            return []
        expected = e.subtotal + e.tax
        result = check_amounts(ctx, expected, e.total, lines=e.line_count)
        if not result.mismatch:
            return []
        return [
            make_issue(
                self,
                "total",
                f"Subtotal {money(ctx, e.subtotal)} + tax {money(ctx, e.tax)} = {money(ctx, expected)}, "
                f"but total is {money(ctx, e.total)} (difference {money(ctx, result.difference)}).",
                expected=money(ctx, expected),
                actual=money(ctx, e.total),
            )
        ]
