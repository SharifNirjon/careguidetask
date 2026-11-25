"""Dates & formats."""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar

from app.extraction.normalize import ISO_4217
from app.models.enums import RuleCode, Severity
from app.validation.rules.base import REASON_UNPARSEABLE, make_issue
from app.validation.types import Issue, ValidationContext


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February in a non-leap target year
        return day.replace(year=day.year - years, day=28)


class InvalidDate:
    code: ClassVar = RuleCode.INVALID_DATE
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.meta("invoice_date").get("reason") == REASON_UNPARSEABLE

    def check(self, ctx: ValidationContext) -> list[Issue]:
        meta = ctx.extraction.meta("invoice_date")
        note = f" ({meta['note']})" if meta.get("note") else ""
        return [
            make_issue(
                self,
                "invoice_date",
                f"Invoice date {meta['rejected_value']!r} could not be read{note}.",
                actual=str(meta["rejected_value"]),
            )
        ]


class FutureDate:
    code: ClassVar = RuleCode.FUTURE_DATE
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.invoice_date is not None

    def check(self, ctx: ValidationContext) -> list[Issue]:
        invoice_date = ctx.extraction.invoice_date
        if invoice_date is None:
            return []
        limit = ctx.today + timedelta(days=ctx.settings.future_date_grace_days)
        if invoice_date <= limit:
            return []
        return [
            make_issue(
                self,
                "invoice_date",
                f"Invoice date {invoice_date.isoformat()} is in the future "
                f"(today {ctx.today.isoformat()}, grace {ctx.settings.future_date_grace_days} days).",
                actual=invoice_date.isoformat(),
                expected=f"<= {limit.isoformat()}",
            )
        ]


class DateTooOld:
    code: ClassVar = RuleCode.DATE_TOO_OLD
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.invoice_date is not None

    def check(self, ctx: ValidationContext) -> list[Issue]:
        invoice_date = ctx.extraction.invoice_date
        if invoice_date is None:
            return []
        years = ctx.settings.max_invoice_age_years
        oldest = years_before(ctx.today, years)
        if invoice_date >= oldest:
            return []
        return [
            make_issue(
                self,
                "invoice_date",
                f"Invoice date {invoice_date.isoformat()} is more than {years} years old.",
                actual=invoice_date.isoformat(),
                expected=f">= {oldest.isoformat()}",
            )
        ]


class UnknownCurrency:
    """Not ISO 4217, or a symbol that couldn't be resolved ('$' with no code printed)."""

    code: ClassVar = RuleCode.UNKNOWN_CURRENCY
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        e = ctx.extraction
        return e.currency is not None or e.meta("currency").get("reason") == REASON_UNPARSEABLE

    def check(self, ctx: ValidationContext) -> list[Issue]:
        e = ctx.extraction
        if e.currency is not None:
            if e.currency in ISO_4217:
                return []
            return [
                make_issue(
                    self, "currency", f"Currency {e.currency!r} is not an ISO 4217 code.", actual=e.currency
                )
            ]
        meta = e.meta("currency")
        note = f" ({meta['note']})" if meta.get("note") else ""
        return [
            make_issue(
                self,
                "currency",
                f"Currency {meta.get('rejected_value')!r} could not be resolved to an ISO 4217 code{note}.",
                actual=str(meta.get("rejected_value")),
            )
        ]


class InvoiceNumberFormat:
    code: ClassVar = RuleCode.INVOICE_NUMBER_FORMAT
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.invoice_number is not None

    def check(self, ctx: ValidationContext) -> list[Issue]:
        e = ctx.extraction
        key = e.invoice_number_norm or ""
        low, high = ctx.settings.invoice_number_min_len, ctx.settings.invoice_number_max_len
        if low <= len(key) <= high:
            return []
        reason = (
            "has no letters or digits"
            if not key
            else f"normalizes to {len(key)} characters ({low}-{high} expected)"
        )
        return [
            make_issue(
                self,
                "invoice_number",
                f"Invoice number {e.invoice_number!r} {reason}.",
                actual=e.invoice_number,
            )
        ]
