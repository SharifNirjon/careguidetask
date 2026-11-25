"""Duplicates & cross-document checks over PRE-FETCHED candidates (no DB here)."""

from __future__ import annotations

from typing import ClassVar, Final

from app.models.enums import RuleCode, Severity
from app.validation.crossdoc import classify, compare_key_fields
from app.validation.rules.base import make_issue
from app.validation.types import Candidate, Issue, ValidationContext

SEVERITIES: Final = {
    RuleCode.DUPLICATE_INVOICE: Severity.ERROR,
    RuleCode.INVOICE_NUMBER_CONFLICT: Severity.ERROR,
    RuleCode.CROSS_DOC_VERIFIED: Severity.INFO,
    RuleCode.CROSS_DOC_MISMATCH: Severity.ERROR,
    RuleCode.POSSIBLE_DUPLICATE: Severity.WARNING,
}


def _describe(candidate: Candidate) -> str:
    other = candidate.extraction
    number = other.invoice_number or "(no number)"
    return f"invoice {number} in '{candidate.document.filename}'"


class DuplicateFile:
    """The very same bytes were uploaded before (informational: the invoice-level
    rules decide whether it matters)."""

    code: ClassVar = RuleCode.DUPLICATE_FILE
    default_severity: ClassVar = Severity.INFO

    def applies(self, ctx: ValidationContext) -> bool:
        return bool(ctx.candidates)

    def check(self, ctx: ValidationContext) -> list[Issue]:
        seen: set[str] = set()
        issues = []
        for candidate in ctx.candidates:
            document = candidate.document
            if document.sha256 == ctx.document.sha256 and str(document.id) not in seen:
                seen.add(str(document.id))
                issues.append(
                    make_issue(
                        self,
                        None,
                        f"The same file was also uploaded as '{document.filename}'.",
                        related=candidate.extraction.id,
                    )
                )
        return issues


class CrossDocument:
    """Exact matches (same invoice number + similar supplier) -> the classification
    table; fuzzy matches -> POSSIBLE_DUPLICATE only."""

    code: ClassVar = RuleCode.DUPLICATE_INVOICE
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        return bool(ctx.candidates)

    def check(self, ctx: ValidationContext) -> list[Issue]:
        issues = []
        for candidate in ctx.candidates:
            comparison = compare_key_fields(ctx.extraction, candidate.extraction, rel_tol=ctx.rel_tol)
            code = classify(
                candidate.kind,
                ctx.document.source_kind,
                candidate.document.source_kind,
                comparison,
                this_is_later=ctx.document.is_later_than(candidate.document),
            )
            if code is None:
                continue
            diffs = "; ".join(f"{d.field}: {d.left} vs {d.right}" for d in comparison.differences)
            issues.append(
                make_issue(
                    self,
                    None,
                    _MESSAGES[code].format(
                        other=_describe(candidate), diffs=diffs, similarity=candidate.supplier_similarity
                    ),
                    code=code,
                    severity=SEVERITIES[code],
                    related=candidate.extraction.id,
                    expected=diffs or None,
                )
            )
        return issues


_MESSAGES: Final = {
    RuleCode.DUPLICATE_INVOICE: "Duplicate of {other}, uploaded earlier, with the same amounts.",
    RuleCode.INVOICE_NUMBER_CONFLICT: (
        "Same invoice number and supplier as {other}, but different details: {diffs}."
    ),
    RuleCode.CROSS_DOC_VERIFIED: "Matches {other} on every compared field.",
    RuleCode.CROSS_DOC_MISMATCH: "Differs from {other}: {diffs}.",
    RuleCode.POSSIBLE_DUPLICATE: "Possible duplicate of {other}: same total, close date, similar supplier "
    "({similarity:.2f}), different or missing invoice number.",
}
