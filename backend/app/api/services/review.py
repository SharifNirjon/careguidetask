"""Human review: approve / reject extractions, resolve / reopen issues.

Resolving or reopening an issue re-runs validation in the same transaction,
so review_status and the document status always reflect the change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.orm import Session

from app.api.auth import Principal
from app.api.container import Container
from app.api.errors import Conflict, NotFound
from app.api.repositories.extractions import ExtractionRepository
from app.api.schemas.results import ExtractionOut, IssueOut
from app.api.services import views
from app.models import Document, Extraction, ValidationIssue
from app.models.enums import ReviewStatus
from app.validation.policy import Trigger
from app.validation.repository import ValidationRepository

APPROVED: Final = "approved"
REJECTED: Final = "rejected"


class ReviewService:
    def __init__(self, session: Session, container: Container) -> None:
        self._session = session
        self._c = container
        self._repo = ExtractionRepository(session)

    def approve(self, extraction_id: uuid.UUID, note: str | None, principal: Principal) -> ExtractionOut:
        extraction = self._current(extraction_id)
        if self._repo.has_open_errors(extraction_id):
            raise Conflict(
                "Unresolved errors remain: correct the values or resolve the issues first.",
                code="unresolved_errors",
            )
        return self._decide(extraction, ReviewStatus.APPROVED, note, principal)

    def reject(self, extraction_id: uuid.UUID, note: str, principal: Principal) -> ExtractionOut:
        return self._decide(self._current(extraction_id), ReviewStatus.REJECTED, note, principal)

    def resolve(self, issue_id: uuid.UUID, note: str, principal: Principal) -> IssueOut:
        issue = self._active_issue(issue_id)
        issue.resolved_at, issue.resolved_by, issue.resolution_note = datetime.now(UTC), principal.name, note
        return self._revalidate(issue)

    def reopen(self, issue_id: uuid.UUID) -> IssueOut:
        issue = self._active_issue(issue_id)
        if issue.resolved_at is None:
            raise Conflict("The issue is not resolved.", code="not_resolved")
        issue.resolved_at = issue.resolved_by = issue.resolution_note = None
        return self._revalidate(issue)

    # ------------------------------------------------------------ helpers

    def _decide(
        self, extraction: Extraction, status: ReviewStatus, note: str | None, principal: Principal
    ) -> ExtractionOut:
        now = datetime.now(UTC)
        extraction.review_status, extraction.reviewed_by, extraction.reviewed_at = status, principal.name, now
        extraction.field_meta = {
            **(extraction.field_meta or {}),
            views.REVIEW_META_KEY: {
                "decision": status.value,
                "note": note,
                "by": principal.name,
                "at": now.isoformat(),
            },
        }
        self._session.flush()
        ValidationRepository(self._session).refresh_document_status(extraction.document_id)
        self._session.commit()
        self._session.refresh(extraction)
        ids = [extraction.id]
        return views.extraction_out(
            extraction, self._repo.active_issues(ids), self._repo.reconciliations(ids)
        )

    def _revalidate(self, issue: ValidationIssue) -> IssueOut:
        self._session.flush()
        self._c.validation_service().validate_in(self._session, issue.extraction_id, trigger=Trigger.MANUAL)
        document_id = self._session.get_one(Extraction, issue.extraction_id).document_id
        ValidationRepository(self._session).refresh_document_status(document_id)
        self._session.commit()
        self._session.refresh(issue)
        return views.issue_out(issue)

    def _current(self, extraction_id: uuid.UUID) -> Extraction:
        extraction = self._repo.get(extraction_id)
        if (
            extraction is None
            or self._session.get_one(Document, extraction.document_id).deleted_at is not None
        ):
            raise NotFound(f"Extraction {extraction_id} was not found.", code="extraction_not_found")
        if not extraction.is_current:
            raise Conflict("Only the current version of an extraction can be reviewed.", code="not_current")
        return extraction

    def _active_issue(self, issue_id: uuid.UUID) -> ValidationIssue:
        issue = self._repo.issue(issue_id)
        if issue is None:
            raise NotFound(f"Issue {issue_id} was not found.", code="issue_not_found")
        if issue.superseded_at is not None:
            raise Conflict("The issue was superseded by a newer validation.", code="issue_superseded")
        return issue
