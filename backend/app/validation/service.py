"""ValidationService: used by the pipeline now and by manual corrections (Step 6).

One transaction per extraction: advisory lock -> load -> find candidates ->
pure rules -> issue diff -> reconciliation upserts -> review status ->
re-validate counterparts. Either all of it commits or none of it does.

Why the advisory lock: two copies of one invoice processed at the same time
both see each other (their extractions are committed before validation), so
both would write issues for BOTH sides at once and race on the fingerprint
diff. pg_advisory_xact_lock(hashtext(invoice_number_norm)) serializes every
validation that touches one invoice number; the second waits, then sees the
first one's rows. The unique active-fingerprint index is the backstop for
matches that can't share a key (fuzzy matches by supplier similarity): a
collision there is retried once.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import ValidationSettings
from app.models import DocumentStatus, ReviewStatus
from app.models.enums import Severity
from app.validation.crossdoc import compare_key_fields
from app.validation.lifecycle import plan_issues
from app.validation.policy import Trigger, decide_review_status
from app.validation.repository import ValidationRepository, lock_key
from app.validation.rules import run_rules
from app.validation.types import Issue, ValidationContext

MS_PER_S: Final = 1000
SessionFactory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    extraction_id: uuid.UUID
    issues: list[Issue]
    review_status: ReviewStatus
    duration_ms: float  # rule evaluation only (DB I/O excluded)
    counterparts: list[uuid.UUID] = field(default_factory=list)
    rows_changed: bool = False


@dataclass(frozen=True, slots=True)
class DocumentValidation:
    document_id: uuid.UUID
    outcomes: list[ValidationOutcome]
    document_status: DocumentStatus

    def counts(self) -> dict[str, dict[str, int]]:
        """processing_events details: issues by severity and by rule."""
        by_severity = {s.value: 0 for s in Severity}
        by_rule: dict[str, int] = {}
        for outcome in self.outcomes:
            for issue in outcome.issues:
                by_severity[issue.severity.value] += 1
                by_rule[issue.rule_code.value] = by_rule.get(issue.rule_code.value, 0) + 1
        return {"by_severity": by_severity, "by_rule": dict(sorted(by_rule.items()))}


class ValidationService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        settings: ValidationSettings,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._today = today or (lambda: datetime.now(UTC).date())

    def validate_extraction(
        self, extraction_id: uuid.UUID, *, trigger: Trigger = Trigger.PIPELINE
    ) -> ValidationOutcome:
        try:
            return self._validate_once(extraction_id, trigger)
        except IntegrityError:  # a concurrent run inserted the same active fingerprint first
            return self._validate_once(extraction_id, trigger)

    def _validate_once(self, extraction_id: uuid.UUID, trigger: Trigger) -> ValidationOutcome:
        with self._session() as session:
            return self._validate(session, extraction_id, trigger, propagate=True)

    def validate_document(
        self, document_id: uuid.UUID, *, trigger: Trigger = Trigger.PIPELINE
    ) -> DocumentValidation:
        """Every current extraction (each in its own transaction), then the document status."""
        with self._session() as session:
            ids = ValidationRepository(session).current_extraction_ids(document_id)
        outcomes = [self.validate_extraction(i, trigger=trigger) for i in ids]
        with self._session() as session:
            status = ValidationRepository(session).refresh_document_status(document_id)
        return DocumentValidation(document_id, outcomes, status)

    def revalidate_document(self, document_id: uuid.UUID) -> DocumentValidation:
        """For Step 6 (rules/settings changed, reviewer asked for a re-check)."""
        return self.validate_document(document_id, trigger=Trigger.MANUAL)

    def _validate(
        self, session: Session, extraction_id: uuid.UUID, trigger: Trigger, *, propagate: bool
    ) -> ValidationOutcome:
        repo = ValidationRepository(session)
        loaded = repo.load(extraction_id)
        if propagate:
            repo.lock(lock_key(loaded.snapshot))
        candidates = repo.find_candidates(loaded.snapshot, self._settings)
        ctx = ValidationContext(
            extraction=loaded.snapshot,
            document=loaded.document,
            today=self._today(),
            settings=self._settings,
            candidates=tuple(candidates),
        )
        started = time.perf_counter()
        issues = run_rules(ctx)
        duration_ms = (time.perf_counter() - started) * MS_PER_S

        plan = plan_issues(repo.active_issues(extraction_id), issues)
        repo.apply(extraction_id, plan, datetime.now(UTC))
        for candidate in candidates:
            comparison = compare_key_fields(loaded.snapshot, candidate.extraction, rel_tol=ctx.rel_tol)
            repo.upsert_reconciliation(loaded.snapshot, candidate, comparison)
        status = decide_review_status(
            loaded.review_status,
            plan.states(),
            loaded.snapshot.overall_confidence,
            trigger=trigger,
            settings=self._settings,
        )
        status_changed = repo.set_review_status(extraction_id, status)

        counterparts: list[uuid.UUID] = []
        if propagate:  # B matched A: A gets its side of the story too (no further cascade)
            for candidate in candidates:
                self._validate(session, candidate.extraction.id, Trigger.COUNTERPART, propagate=False)
                repo.refresh_document_status(candidate.document.id)
                counterparts.append(candidate.extraction.id)
        return ValidationOutcome(
            extraction_id, issues, status, duration_ms, counterparts, plan.changes_rows or status_changed
        )

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()
