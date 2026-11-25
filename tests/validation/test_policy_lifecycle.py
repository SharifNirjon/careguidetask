"""Review decision policy and the issue-lifecycle diff (pure)."""

from __future__ import annotations

import uuid

import pytest

from app.config import ValidationSettings
from app.models.enums import DocumentStatus, ReviewStatus, RuleCode, Severity
from app.validation.lifecycle import ActiveIssue, plan_issues
from app.validation.policy import IssueState, Trigger, decide_review_status, document_status
from app.validation.types import Issue

SETTINGS = ValidationSettings()
ERROR = IssueState("TOTAL_MISMATCH", Severity.ERROR, False)
RESOLVED_ERROR = IssueState("TOTAL_MISMATCH", Severity.ERROR, True)
BLOCKING = IssueState("LOW_CONFIDENCE", Severity.WARNING, False)
HARMLESS = IssueState("ZERO_TOTAL", Severity.WARNING, False)
INFO = IssueState("CROSS_DOC_VERIFIED", Severity.INFO, False)


@pytest.mark.parametrize(
    ("current", "issues", "confidence", "trigger", "expected"),
    [
        (ReviewStatus.PENDING, [], 0.95, Trigger.PIPELINE, ReviewStatus.AUTO_APPROVED),
        (ReviewStatus.PENDING, [INFO, HARMLESS], 0.95, Trigger.PIPELINE, ReviewStatus.AUTO_APPROVED),
        (ReviewStatus.PENDING, [ERROR], 0.99, Trigger.PIPELINE, ReviewStatus.NEEDS_REVIEW),
        (ReviewStatus.PENDING, [RESOLVED_ERROR], 0.95, Trigger.PIPELINE, ReviewStatus.AUTO_APPROVED),
        (ReviewStatus.PENDING, [BLOCKING], 0.95, Trigger.PIPELINE, ReviewStatus.NEEDS_REVIEW),
        (ReviewStatus.PENDING, [], 0.84, Trigger.PIPELINE, ReviewStatus.NEEDS_REVIEW),
        (ReviewStatus.PENDING, [], 0.85, Trigger.PIPELINE, ReviewStatus.AUTO_APPROVED),
        (ReviewStatus.PENDING, [], None, Trigger.PIPELINE, ReviewStatus.NEEDS_REVIEW),
        # human decisions stand...
        (ReviewStatus.APPROVED, [ERROR], 0.5, Trigger.PIPELINE, ReviewStatus.APPROVED),
        (ReviewStatus.REJECTED, [], 0.99, Trigger.COUNTERPART, ReviewStatus.REJECTED),
        (ReviewStatus.APPROVED, [ERROR], 0.5, Trigger.MANUAL, ReviewStatus.APPROVED),
        # ...unless a correction changed a field
        (ReviewStatus.APPROVED, [ERROR], 0.95, Trigger.CORRECTION, ReviewStatus.NEEDS_REVIEW),
        (ReviewStatus.AUTO_APPROVED, [ERROR], 0.95, Trigger.COUNTERPART, ReviewStatus.NEEDS_REVIEW),
    ],
)
def test_review_policy(
    current: ReviewStatus,
    issues: list[IssueState],
    confidence: float | None,
    trigger: Trigger,
    expected: ReviewStatus,
) -> None:
    assert decide_review_status(current, issues, confidence, trigger=trigger, settings=SETTINGS) is expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([ReviewStatus.AUTO_APPROVED, ReviewStatus.APPROVED], DocumentStatus.COMPLETED),
        ([ReviewStatus.AUTO_APPROVED, ReviewStatus.NEEDS_REVIEW], DocumentStatus.NEEDS_REVIEW),
        ([ReviewStatus.REJECTED], DocumentStatus.COMPLETED),
        ([], DocumentStatus.COMPLETED),
    ],
)
def test_document_status(statuses: list[ReviewStatus], expected: DocumentStatus) -> None:
    assert document_status(statuses) is expected


def issue(code: RuleCode = RuleCode.TOTAL_MISMATCH, actual: str = "120.00", message: str = "m") -> Issue:
    return Issue(code, Severity.ERROR, "total", message, "115.00", actual)


def active_from(i: Issue, *, resolved: bool = False) -> ActiveIssue:
    return ActiveIssue(
        uuid.uuid4(),
        i.rule_code.value,
        i.fingerprint,
        i.severity,
        i.message,
        i.expected_value,
        i.actual_value,
        resolved,
    )


def test_unchanged_issue_is_kept_untouched() -> None:
    existing = active_from(issue(), resolved=True)
    plan = plan_issues([existing], [issue()])
    assert plan.keep == [existing] and not plan.changes_rows
    assert plan.states() == [IssueState("TOTAL_MISMATCH", Severity.ERROR, True)]  # still resolved


def test_new_issue_is_inserted_and_gone_issue_superseded() -> None:
    old = active_from(issue(RuleCode.ZERO_TOTAL))
    plan = plan_issues([old], [issue()])
    assert [i.rule_code for i in plan.insert] == [RuleCode.TOTAL_MISMATCH]
    assert plan.supersede == [old.id]


def test_changed_wording_keeps_the_resolution() -> None:
    existing = active_from(issue(message="old wording"), resolved=True)
    plan = plan_issues([existing], [issue(message="new wording")])
    assert plan.update[0].id == existing.id and not plan.update[0].reopen and plan.update[0].resolved


def test_changed_actual_value_reopens_a_resolved_issue() -> None:
    existing = active_from(issue(actual="120.00"), resolved=True)
    plan = plan_issues([existing], [issue(actual="130.00")])
    assert plan.update[0].reopen and not plan.update[0].resolved
    assert plan.states() == [IssueState("TOTAL_MISMATCH", Severity.ERROR, False)]


def test_duplicate_fingerprints_in_the_new_set_are_collapsed() -> None:
    plan = plan_issues([], [issue(), issue(message="same fingerprint")])
    assert len(plan.insert) == 1
