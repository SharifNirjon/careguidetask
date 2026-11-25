"""Completeness & trust: is everything there, and can we believe it?"""

from __future__ import annotations

from typing import ClassVar

from app.models.enums import RuleCode, Severity
from app.validation.rules.base import HEADER_FIELDS, REASON_UNGROUNDED, iter_field_metas, label, make_issue
from app.validation.types import Issue, ValidationContext

OTHER_DOCUMENT_TYPE = "other"


class MissingField:
    """Required fields per document_type (settings). A field whose value was
    REJECTED is not reported here: UNGROUNDED_VALUE / INVALID_* already say why."""

    code: ClassVar = RuleCode.MISSING_FIELD
    default_severity: ClassVar = Severity.ERROR

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.document_type != OTHER_DOCUMENT_TYPE

    def check(self, ctx: ValidationContext) -> list[Issue]:
        required = ctx.settings.required_fields.get(ctx.extraction.document_type, {})
        issues = []
        for name, severity in required.items():
            extraction = ctx.extraction
            if extraction.value(name) is None and extraction.meta(name).get("rejected_value") is None:
                issues.append(
                    make_issue(self, name, f"{label(name)} is missing.", severity=Severity(severity))
                )
        return issues


class UngroundedValue:
    """The model gave a value we could not find in the cited text (or sheet rows
    disagreed): it was nulled, and the reviewer must see what was rejected."""

    code: ClassVar = RuleCode.UNGROUNDED_VALUE
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        return True

    def check(self, ctx: ValidationContext) -> list[Issue]:
        issues = []
        for path, _name, meta in iter_field_metas(ctx):
            if meta.get("rejected_value") is None or meta.get("reason") not in REASON_UNGROUNDED:
                continue
            severity = Severity.ERROR if path in ctx.settings.ungrounded_error_fields else Severity.WARNING
            rejected = meta["rejected_value"]
            why = (
                "rows disagree" if meta.get("reason") == "conflicting_rows" else "not found in the cited text"
            )
            issues.append(
                make_issue(
                    self,
                    path,
                    f"{label(path)} {rejected!r} was rejected: {why}.",
                    severity=severity,
                    actual=str(rejected),
                )
            )
        return issues


class LowConfidence:
    code: ClassVar = RuleCode.LOW_CONFIDENCE
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        return True

    def check(self, ctx: ValidationContext) -> list[Issue]:
        minimum = ctx.settings.field_confidence_min
        issues = []
        for name in HEADER_FIELDS:
            confidence = ctx.extraction.meta(name).get("confidence")
            if ctx.extraction.value(name) is not None and confidence is not None and confidence < minimum:
                issues.append(
                    make_issue(
                        self,
                        name,
                        f"{label(name)} has low confidence ({confidence:.2f} < {minimum:.2f}).",
                        actual=f"{confidence:.3f}",
                        expected=f">= {minimum:.2f}",
                    )
                )
        overall = ctx.extraction.overall_confidence
        if overall is not None and overall < ctx.settings.overall_confidence_min:
            issues.append(
                make_issue(
                    self,
                    "overall_confidence",
                    f"Overall confidence is low ({overall:.2f} < {ctx.settings.overall_confidence_min:.2f}).",
                    actual=f"{overall:.3f}",
                    expected=f">= {ctx.settings.overall_confidence_min:.2f}",
                )
            )
        return issues


class NotAnInvoice:
    code: ClassVar = RuleCode.NOT_AN_INVOICE
    default_severity: ClassVar = Severity.WARNING

    def applies(self, ctx: ValidationContext) -> bool:
        return ctx.extraction.document_type == OTHER_DOCUMENT_TYPE

    def check(self, ctx: ValidationContext) -> list[Issue]:
        return [make_issue(self, None, "This does not look like an invoice, receipt or statement.")]
