"""Issue lifecycle as a fingerprint diff, so a reviewer's work is never lost.

  same fingerprint, same content   -> keep the row untouched (id + resolution kept)
  same fingerprint, new content    -> refresh message/values in place; a RESOLVED
                                      issue is reopened only if its actual value
                                      changed (the reviewer acknowledged a different number)
  new fingerprint                  -> insert
  fingerprint gone                 -> superseded (fixed by a correction / re-extraction)

Issues belong to one extraction VERSION: a new version starts with no active
issues, so a reappearing issue there is (correctly) a new one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.models.enums import Severity
from app.validation.policy import IssueState
from app.validation.types import Issue


@dataclass(frozen=True, slots=True)
class ActiveIssue:
    id: uuid.UUID
    rule_code: str
    fingerprint: str
    severity: Severity
    message: str
    expected_value: str | None
    actual_value: str | None
    resolved: bool

    def same_content(self, issue: Issue) -> bool:
        return (self.severity, self.message, self.expected_value, self.actual_value) == (
            issue.severity,
            issue.message,
            issue.expected_value,
            issue.actual_value,
        )


@dataclass(frozen=True, slots=True)
class IssueUpdate:
    id: uuid.UUID
    issue: Issue
    reopen: bool  # clear the resolution (the reviewer acknowledged a different value)
    resolved: bool  # still resolved after the update


@dataclass(slots=True)
class IssuePlan:
    insert: list[Issue] = field(default_factory=list)
    update: list[IssueUpdate] = field(default_factory=list)
    supersede: list[uuid.UUID] = field(default_factory=list)
    keep: list[ActiveIssue] = field(default_factory=list)

    @property
    def changes_rows(self) -> bool:
        return bool(self.insert or self.update or self.supersede)

    def states(self) -> list[IssueState]:
        """The active issues after applying the plan (input to the review policy)."""
        return [
            *(IssueState(a.rule_code, a.severity, a.resolved) for a in self.keep),
            *(IssueState(u.issue.rule_code.value, u.issue.severity, u.resolved) for u in self.update),
            *(IssueState(i.rule_code.value, i.severity, False) for i in self.insert),
        ]


def plan_issues(active: Sequence[ActiveIssue], new: Sequence[Issue]) -> IssuePlan:
    plan = IssuePlan()
    by_fingerprint = {a.fingerprint: a for a in active}
    seen: set[str] = set()
    for issue in new:
        fingerprint = issue.fingerprint
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        existing = by_fingerprint.get(fingerprint)
        if existing is None:
            plan.insert.append(issue)
        elif existing.same_content(issue):
            plan.keep.append(existing)
        else:
            reopen = existing.resolved and existing.actual_value != issue.actual_value
            plan.update.append(IssueUpdate(existing.id, issue, reopen, existing.resolved and not reopen))
    plan.supersede = [a.id for fp, a in by_fingerprint.items() if fp not in seen]
    return plan
