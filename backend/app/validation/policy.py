"""Review decision policy: pure, so every outcome is explainable from its inputs."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.config import ValidationSettings
from app.models.enums import DocumentStatus, ReviewStatus, Severity

HUMAN_DECISIONS: Final = frozenset({ReviewStatus.APPROVED, ReviewStatus.REJECTED})


class Trigger(StrEnum):
    PIPELINE = "pipeline"  # a new extraction version from the pipeline
    CORRECTION = "correction"  # a reviewer changed a field (Step 6)
    COUNTERPART = "counterpart"  # another document matched this one
    MANUAL = "manual"  # explicit re-validation (rules or settings changed)


@dataclass(frozen=True, slots=True)
class IssueState:
    rule_code: str
    severity: Severity
    resolved: bool


def decide_review_status(
    current: ReviewStatus,
    issues: Sequence[IssueState],
    overall_confidence: float | None,
    *,
    trigger: Trigger,
    settings: ValidationSettings,
) -> ReviewStatus:
    """- a human decision (approved/rejected) stands, unless a correction changed a field
    - any unresolved error -> needs_review
    - otherwise auto_approved if confidence is high enough and no unresolved
      warning is in BLOCKING_WARNINGS; else needs_review
    Resolved issues (acknowledged by a reviewer) never block."""
    if current in HUMAN_DECISIONS and trigger is not Trigger.CORRECTION:
        return current
    open_issues = [i for i in issues if not i.resolved]
    if any(i.severity is Severity.ERROR for i in open_issues):
        return ReviewStatus.NEEDS_REVIEW
    blocking = set(settings.blocking_warnings)
    if any(i.severity is Severity.WARNING and i.rule_code in blocking for i in open_issues):
        return ReviewStatus.NEEDS_REVIEW
    if overall_confidence is None or overall_confidence < settings.auto_approve_min:
        return ReviewStatus.NEEDS_REVIEW
    return ReviewStatus.AUTO_APPROVED


def document_status(review_statuses: Iterable[ReviewStatus]) -> DocumentStatus:
    """needs_review if any current extraction still needs a human, else completed."""
    if any(status is ReviewStatus.NEEDS_REVIEW for status in review_statuses):
        return DocumentStatus.NEEDS_REVIEW
    return DocumentStatus.COMPLETED
