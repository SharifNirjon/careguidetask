"""Explicit, ordered rule registry (no auto-discovery: the order is the catalog).

Settings can disable a rule (by its code, or by any issue code it emits) and
override severities per issue code.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from app.config import ValidationSettings
from app.models.enums import Severity
from app.validation.rules.arithmetic import LineAmountMismatch, LineSumMismatch, TotalMismatch
from app.validation.rules.base import ValidationRule
from app.validation.rules.completeness import LowConfidence, MissingField, NotAnInvoice, UngroundedValue
from app.validation.rules.dates import (
    DateTooOld,
    FutureDate,
    InvalidDate,
    InvoiceNumberFormat,
    UnknownCurrency,
)
from app.validation.rules.duplicates import CrossDocument, DuplicateFile
from app.validation.rules.numeric import (
    ImplausibleTaxRate,
    InvalidAmount,
    NegativeAmount,
    SignInconsistent,
    ZeroTotal,
)
from app.validation.types import Issue, ValidationContext

RULES: Final[tuple[ValidationRule, ...]] = (
    # completeness & trust
    MissingField(), UngroundedValue(), LowConfidence(), NotAnInvoice(),
    # numeric validity
    InvalidAmount(), NegativeAmount(), SignInconsistent(), ImplausibleTaxRate(), ZeroTotal(),
    # arithmetic consistency
    LineAmountMismatch(), LineSumMismatch(), TotalMismatch(),
    # dates & formats
    InvalidDate(), FutureDate(), DateTooOld(), UnknownCurrency(), InvoiceNumberFormat(),
    # duplicates & cross-document
    DuplicateFile(), CrossDocument(),
)  # fmt: skip


def run_rules(ctx: ValidationContext, rules: tuple[ValidationRule, ...] = RULES) -> list[Issue]:
    """Every enabled rule, in catalog order; one issue per fingerprint."""
    settings: ValidationSettings = ctx.settings
    disabled = set(settings.disabled_rules)
    issues: dict[str, Issue] = {}
    for rule in rules:
        if rule.code.value in disabled or not rule.applies(ctx):
            continue
        for issue in rule.check(ctx):
            if issue.rule_code.value in disabled:
                continue
            override = settings.severity_overrides.get(issue.rule_code.value)
            final = issue if override is None else replace(issue, severity=Severity(override))
            issues.setdefault(final.fingerprint, final)
    return list(issues.values())


__all__ = ["RULES", "ValidationRule", "run_rules"]
