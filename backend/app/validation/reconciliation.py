"""Batch reconciliation: a user checks one set of documents against another
(PDFs vs an Excel ledger, or PDFs vs PDFs).

match_sets() is pure. Exact pairing first (same invoice number + similar
supplier), then the fuzzy rule for what's left. One-to-many in either
direction is reported as ambiguous instead of guessing a pairing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.config import ValidationSettings
from app.models import ReconciliationRun, RunStatus
from app.validation.crossdoc import KeyComparison, compare_key_fields, trigram_similarity, within_days
from app.validation.repository import ValidationRepository
from app.validation.types import Candidate, DocumentInfo, ExtractionSnapshot, MatchKind

SessionFactory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class Entry:
    extraction: ExtractionSnapshot
    document: DocumentInfo

    def to_dict(self) -> dict[str, Any]:
        e = self.extraction
        return {
            "extraction_id": str(e.id),
            "document_id": str(self.document.id),
            "filename": self.document.filename,
            "invoice_number": e.invoice_number,
            "supplier": e.supplier_name,
            "total": None if e.total is None else str(e.total),
        }


@dataclass(frozen=True, slots=True)
class Pair:
    left: Entry
    right: Entry
    kind: MatchKind
    similarity: float
    comparison: KeyComparison

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "match": self.kind.value,
            "supplier_similarity": round(self.similarity, 3),
            "differences": [d.to_dict() for d in self.comparison.differences],
            "not_compared": list(self.comparison.not_compared),
        }


@dataclass(slots=True)
class BatchReport:
    matched: list[Pair] = field(default_factory=list)
    mismatched: list[Pair] = field(default_factory=list)
    only_in_left: list[Entry] = field(default_factory=list)
    only_in_right: list[Entry] = field(default_factory=list)
    ambiguous: list[dict[str, list[Entry]]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "counts": {
                "matched": len(self.matched),
                "mismatched": len(self.mismatched),
                "only_in_left": len(self.only_in_left),
                "only_in_right": len(self.only_in_right),
                "ambiguous": len(self.ambiguous),
            },
            "matched": [p.to_dict() for p in self.matched],
            "mismatched": [p.to_dict() for p in self.mismatched],
            "only_in_left": [e.to_dict() for e in self.only_in_left],
            "only_in_right": [e.to_dict() for e in self.only_in_right],
            "ambiguous": [
                {side: [e.to_dict() for e in entries] for side, entries in group.items()}
                for group in self.ambiguous
            ],
        }


Option = tuple[Entry, MatchKind, float]


def _similar(left: Entry, rights: Sequence[Entry], minimum: float) -> list[tuple[Entry, float]]:
    scored = [
        (r, trigram_similarity(left.extraction.supplier_name_norm, r.extraction.supplier_name_norm))
        for r in rights
    ]
    return [(r, score) for r, score in scored if score >= minimum]


def _exact(left: Entry, rights: Sequence[Entry], settings: ValidationSettings) -> list[Option]:
    number = left.extraction.invoice_number_norm
    if not number:
        return []
    return [
        (r, MatchKind.EXACT, s)
        for r, s in _similar(left, rights, settings.supplier_match_min)
        if r.extraction.invoice_number_norm == number
    ]


def _fuzzy(left: Entry, rights: Sequence[Entry], settings: ValidationSettings) -> list[Option]:
    a = left.extraction
    if a.total is None:
        return []
    return [
        (r, MatchKind.FUZZY, s)
        for r, s in _similar(left, rights, settings.supplier_match_min)
        if r.extraction.total == a.total
        and within_days(a, r.extraction, settings.duplicate_date_window_days)
        and r.extraction.invoice_number_norm != a.invoice_number_norm
    ]


def _options(
    left: Sequence[Entry], right: Sequence[Entry], settings: ValidationSettings
) -> dict[uuid.UUID, list[Option]]:
    """Exact pairing across the WHOLE set first; fuzzy only for lefts without an
    exact partner, and only against rights nobody claimed exactly."""
    options = {entry.extraction.id: _exact(entry, right, settings) for entry in left}
    exactly_claimed = {r.extraction.id for found in options.values() for r, _, _ in found}
    free_rights = [r for r in right if r.extraction.id not in exactly_claimed]
    for entry in left:
        if not options[entry.extraction.id]:
            options[entry.extraction.id] = _fuzzy(entry, free_rights, settings)
    return options


def match_sets(left: Sequence[Entry], right: Sequence[Entry], settings: ValidationSettings) -> BatchReport:
    rel_tol = Decimal(str(settings.validation_rel_tol))
    report = BatchReport()
    options = _options(left, right, settings)
    claimed_by: dict[uuid.UUID, list[Entry]] = {}
    for entry in left:
        for r, _, _ in options[entry.extraction.id]:
            claimed_by.setdefault(r.extraction.id, []).append(entry)

    seen_groups: set[tuple[uuid.UUID, ...]] = set()
    for entry in left:
        found = options[entry.extraction.id]
        if not found:
            report.only_in_left.append(entry)
            continue
        rights = [r for r, _, _ in found]
        claimants = {c.extraction.id: c for r in rights for c in claimed_by[r.extraction.id]}
        if len(rights) > 1 or len(claimants) > 1:
            key = tuple(sorted([*claimants, *(r.extraction.id for r in rights)]))
            if key not in seen_groups:
                seen_groups.add(key)
                report.ambiguous.append({"left": list(claimants.values()), "right": rights})
            continue
        r, kind, similarity = found[0]
        comparison = compare_key_fields(entry.extraction, r.extraction, rel_tol=rel_tol)
        pair = Pair(entry, r, kind, similarity, comparison)
        (report.matched if comparison.equal else report.mismatched).append(pair)
    report.only_in_right = [r for r in right if r.extraction.id not in claimed_by]
    return report


class ReconciliationService:
    def __init__(self, *, session_factory: SessionFactory, settings: ValidationSettings) -> None:
        self._session_factory = session_factory
        self._settings = settings

    def create_run(
        self,
        left_document_ids: Sequence[uuid.UUID],
        right_document_ids: Sequence[uuid.UUID],
        actor: str | None,
    ) -> uuid.UUID:
        """Record the request first: the caller gets a run id at once (202), and a
        crash later still leaves a visible run."""
        with self._session() as session:
            run = ReconciliationRun(
                left_document_ids=list(left_document_ids),
                right_document_ids=list(right_document_ids),
                created_by=actor,
            )
            session.add(run)
            session.flush()
            return run.id

    def execute(self, run_id: uuid.UUID) -> BatchReport:
        """Match, upsert the pair rows with run_id, store the report (or 'failed')."""
        try:
            with self._session() as session:
                run = session.get_one(ReconciliationRun, run_id)
                repo = ValidationRepository(session)
                left = [Entry(s, d) for s, d in repo.current_entries(run.left_document_ids)]
                right = [Entry(s, d) for s, d in repo.current_entries(run.right_document_ids)]
                report = match_sets(left, right, self._settings)
                for pair in [*report.matched, *report.mismatched]:
                    other = Candidate(pair.right.extraction, pair.right.document, pair.similarity, pair.kind)
                    repo.upsert_reconciliation(pair.left.extraction, other, pair.comparison, run_id=run_id)
                run.summary = report.summary()
                run.status, run.finished_at = RunStatus.SUCCEEDED, datetime.now(UTC)
        except Exception as exc:
            with self._session() as session:
                failed = session.get_one(ReconciliationRun, run_id)
                failed.status, failed.finished_at = RunStatus.FAILED, datetime.now(UTC)
                failed.summary = {"error": type(exc).__name__}
            raise
        return report

    def run(
        self,
        left_document_ids: Sequence[uuid.UUID],
        right_document_ids: Sequence[uuid.UUID],
        actor: str | None,
    ) -> tuple[uuid.UUID, BatchReport]:
        """create_run + execute, inline."""
        run_id = self.create_run(left_document_ids, right_document_ids, actor)
        return run_id, self.execute(run_id)

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
