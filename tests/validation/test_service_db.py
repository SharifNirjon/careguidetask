"""ValidationService against real Postgres: duplicates, cross-doc, lifecycle, concurrency."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import ValidationSettings
from app.models import (
    Document,
    DocumentStatus,
    Extraction,
    Reconciliation,
    ReviewStatus,
    RuleCode,
    ValidationIssue,
    issue_fingerprint,
)
from app.models.enums import ReconciliationStatus, Severity
from app.validation.policy import Trigger
from app.validation.service import ValidationService
from tests.validation.dbseed import XLSX, add_document, add_invoice

TODAY = date(2024, 4, 1)


@pytest.fixture
def sessions(committed_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=committed_engine, expire_on_commit=False)


@pytest.fixture
def service(sessions: sessionmaker[Session]) -> ValidationService:
    return ValidationService(session_factory=sessions, settings=ValidationSettings(), today=lambda: TODAY)


def seed(sessions: sessionmaker[Session], build: Callable[[Session], object]) -> object:
    with sessions() as session, session.begin():
        return build(session)


def active(sessions: sessionmaker[Session], extraction_id: uuid.UUID) -> list[ValidationIssue]:
    with sessions() as session:
        return list(
            session.scalars(
                select(ValidationIssue).where(
                    ValidationIssue.extraction_id == extraction_id, ValidationIssue.superseded_at.is_(None)
                )
            )
        )


def codes(sessions: sessionmaker[Session], extraction_id: uuid.UUID) -> set[RuleCode]:
    return {i.rule_code for i in active(sessions, extraction_id)}


def review_status(sessions: sessionmaker[Session], extraction_id: uuid.UUID) -> ReviewStatus:
    with sessions() as session:
        return session.get_one(Extraction, extraction_id).review_status


def two_copies(sessions: sessionmaker[Session], **later: object) -> tuple[uuid.UUID, uuid.UUID]:
    def build(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
        first = add_invoice(session, add_document(session, name="march.pdf", minutes=0))
        second = add_invoice(session, add_document(session, name="march (1).pdf", minutes=5), **later)  # type: ignore[arg-type]
        return first, second

    return seed(sessions, build)  # type: ignore[return-value]


def test_exact_duplicate_across_two_pdfs(sessions, service) -> None:
    earlier, later = two_copies(sessions)
    outcome = service.validate_extraction(later)
    duplicate = [i for i in active(sessions, later) if i.rule_code is RuleCode.DUPLICATE_INVOICE]
    assert duplicate[0].severity is Severity.ERROR and duplicate[0].related_extraction_id == earlier
    assert "'march.pdf'" in duplicate[0].message
    assert outcome.counterparts == [earlier]
    assert codes(sessions, earlier) == set()  # the original is not the duplicate
    assert (review_status(sessions, later), review_status(sessions, earlier)) == (
        ReviewStatus.NEEDS_REVIEW,
        ReviewStatus.AUTO_APPROVED,
    )
    with sessions() as session:
        reconciliation = session.scalars(select(Reconciliation)).one()
        assert reconciliation.status is ReconciliationStatus.MATCHED and reconciliation.differences == []


def test_supplier_legal_suffix_variants_match(sessions, service) -> None:
    _, later = two_copies(sessions, supplier="Acme Supplies Limited")
    service.validate_extraction(later)
    assert RuleCode.DUPLICATE_INVOICE in codes(sessions, later)


def test_different_supplier_does_not_match(sessions, service) -> None:
    _, later = two_copies(sessions, supplier="Globex Corporation")
    service.validate_extraction(later)
    assert codes(sessions, later) == set()


def test_same_number_different_amounts_conflicts_on_both(sessions, service) -> None:
    earlier, later = two_copies(sessions, total="280.00", subtotal="235.00")
    service.validate_extraction(later)
    for side in (earlier, later):
        assert RuleCode.INVOICE_NUMBER_CONFLICT in codes(sessions, side)


def test_versions_of_the_same_document_never_match(sessions, service) -> None:
    def build(session: Session) -> uuid.UUID:
        document = add_document(session)
        add_invoice(session, document)
        return add_invoice(session, document)  # version 2 of invoice_index 0

    current = seed(sessions, build)
    outcome = service.validate_extraction(current)  # type: ignore[arg-type]
    assert outcome.counterparts == [] and outcome.issues == []


def test_soft_deleted_documents_are_ignored(sessions, service) -> None:
    earlier, later = two_copies(sessions)
    with sessions() as session, session.begin():
        doc_id = session.get_one(Extraction, earlier).document_id
        session.execute(update(Document).where(Document.id == doc_id).values(deleted_at=datetime.now(UTC)))
    service.validate_extraction(later)
    assert codes(sessions, later) == set()


def test_pdf_vs_spreadsheet_verified_and_mismatched(sessions, service) -> None:
    def build(session: Session) -> tuple[uuid.UUID, ...]:
        ledger = add_document(session, name="ledger.xlsx", mime=XLSX, minutes=1)
        verified_pdf = add_invoice(session, add_document(session, name="a.pdf"), number="A-1")
        verified_row = add_invoice(session, ledger, number="A-1", tax=None, subtotal=None, lines=[])
        # a different date than A-1: otherwise a.pdf/b.pdf are (rightly) a fuzzy POSSIBLE_DUPLICATE
        mismatch_pdf = add_invoice(
            session, add_document(session, name="b.pdf"), number="B-2", issued=date(2024, 3, 20)
        )
        mismatch_row = add_invoice(
            session, ledger, invoice_index=1, number="B-2", total="275.00", lines=[], issued=date(2024, 3, 20)
        )
        return verified_pdf, verified_row, mismatch_pdf, mismatch_row

    verified_pdf, verified_row, mismatch_pdf, mismatch_row = seed(sessions, build)  # type: ignore[misc]
    service.validate_extraction(verified_row)
    service.validate_extraction(mismatch_row)
    assert codes(sessions, verified_pdf) == {RuleCode.CROSS_DOC_VERIFIED}
    # the ledger row also lacks subtotal/tax columns: MISSING_FIELD warnings, never errors
    assert codes(sessions, verified_row) - {RuleCode.MISSING_FIELD} == {RuleCode.CROSS_DOC_VERIFIED}
    assert all(i.severity is not Severity.ERROR for i in active(sessions, verified_row))
    for side in (mismatch_pdf, mismatch_row):
        assert RuleCode.CROSS_DOC_MISMATCH in codes(sessions, side)
    with sessions() as session:
        mismatched = session.scalars(
            select(Reconciliation).where(Reconciliation.status == ReconciliationStatus.MISMATCHED)
        ).one()
    assert {"field": "total", "left": "270.00", "right": "275.00"} in mismatched.differences or {
        "field": "total",
        "left": "275.00",
        "right": "270.00",
    } in mismatched.differences


def test_concurrent_copies_still_flag_the_duplicate(sessions, service) -> None:
    earlier, later = two_copies(sessions)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(extraction_id: uuid.UUID) -> None:
        try:
            barrier.wait()
            service.validate_extraction(extraction_id)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(e,)) for e in (earlier, later)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert RuleCode.DUPLICATE_INVOICE in codes(sessions, later)
    with sessions() as session:  # no doubled active issues from the race
        rows = session.execute(
            select(ValidationIssue.extraction_id, ValidationIssue.fingerprint, func.count())
            .where(ValidationIssue.superseded_at.is_(None))
            .group_by(ValidationIssue.extraction_id, ValidationIssue.fingerprint)
        ).all()
    assert all(count == 1 for *_, count in rows)


def _inconsistent(sessions) -> uuid.UUID:
    def build(session: Session) -> uuid.UUID:
        return add_invoice(session, add_document(session), total="300.00", lines=[])

    return seed(sessions, build)  # type: ignore[return-value]


def test_revalidating_unchanged_extraction_changes_no_rows(sessions, service) -> None:
    _, later = two_copies(sessions, total="300.00", lines=[])
    service.validate_extraction(later)

    def snapshot() -> tuple[object, ...]:
        with sessions() as session:
            return (
                sorted(
                    session.execute(
                        select(ValidationIssue.id, ValidationIssue.message, ValidationIssue.superseded_at)
                    ).all()
                ),
                session.scalars(select(Extraction.updated_at).order_by(Extraction.id)).all(),
                sorted(session.execute(select(Reconciliation.id, Reconciliation.status)).all()),
            )

    before = snapshot()
    again = service.validate_extraction(later)
    assert not again.rows_changed
    assert snapshot() == before


def test_resolved_issue_survives_revalidation_and_stops_blocking(sessions, service) -> None:
    extraction = _inconsistent(sessions)
    service.validate_extraction(extraction)
    issue = next(i for i in active(sessions, extraction) if i.rule_code is RuleCode.TOTAL_MISMATCH)
    assert review_status(sessions, extraction) is ReviewStatus.NEEDS_REVIEW
    with sessions() as session, session.begin():
        session.execute(
            update(ValidationIssue)
            .where(ValidationIssue.id == issue.id)
            .values(
                resolved_at=datetime.now(UTC),
                resolved_by="reviewer",
                resolution_note="shipping fee on page 2",
            )
        )
    service.validate_extraction(extraction, trigger=Trigger.MANUAL)
    kept = next(i for i in active(sessions, extraction) if i.rule_code is RuleCode.TOTAL_MISMATCH)
    assert kept.id == issue.id and kept.resolved_by == "reviewer"
    assert review_status(sessions, extraction) is ReviewStatus.AUTO_APPROVED


def test_correcting_the_value_supersedes_the_issue(sessions, service) -> None:
    extraction = _inconsistent(sessions)
    service.validate_extraction(extraction)
    with sessions() as session, session.begin():
        session.get_one(Extraction, extraction).total = __import__("decimal").Decimal("270.00")
    service.validate_extraction(extraction, trigger=Trigger.CORRECTION)
    assert codes(sessions, extraction) == set()
    with sessions() as session:
        superseded = session.scalars(
            select(ValidationIssue).where(
                ValidationIssue.extraction_id == extraction, ValidationIssue.superseded_at.is_not(None)
            )
        ).all()
    assert [i.rule_code for i in superseded] == [RuleCode.TOTAL_MISMATCH]
    assert review_status(sessions, extraction) is ReviewStatus.AUTO_APPROVED


def test_approved_stays_approved(sessions, service) -> None:
    extraction = _inconsistent(sessions)
    with sessions() as session, session.begin():
        session.get_one(Extraction, extraction).review_status = ReviewStatus.APPROVED
    service.validate_extraction(extraction, trigger=Trigger.MANUAL)
    assert review_status(sessions, extraction) is ReviewStatus.APPROVED
    assert RuleCode.TOTAL_MISMATCH in codes(sessions, extraction)  # still reported, just not re-decided


def test_document_status_follows_extractions(sessions, service) -> None:
    def build(session: Session) -> uuid.UUID:
        document = add_document(session)
        add_invoice(session, document)
        add_invoice(session, document, invoice_index=1, number="INV-2", total="999.00", lines=[])
        return document.id

    document_id = seed(sessions, build)
    result = service.validate_document(document_id)  # type: ignore[arg-type]
    assert result.document_status is DocumentStatus.NEEDS_REVIEW
    assert result.counts()["by_rule"] == {"TOTAL_MISMATCH": 1}
    with sessions() as session:
        assert session.get_one(Document, document_id).status is DocumentStatus.NEEDS_REVIEW


def test_rule_evaluation_is_fast(sessions, service) -> None:
    extraction = _inconsistent(sessions)
    assert service.validate_extraction(extraction).duration_ms < 50


def test_sql_backfill_matches_python_fingerprint(sessions) -> None:
    related = uuid.uuid4()
    with sessions() as session:
        sql = session.scalar(
            text(
                "SELECT encode(sha256(convert_to(:code || '|' || coalesce(:path, '') || '|' || "
                "coalesce(CAST(:related AS uuid)::text, ''), 'UTF8')), 'hex')"
            ),
            {"code": "TOTAL_MISMATCH", "path": "line_items[2].amount", "related": str(related)},
        )
        none = session.scalar(
            text(
                "SELECT encode(sha256(convert_to('MISSING_FIELD' || '|' || coalesce(NULL, '') || '|' || "
                "coalesce(NULL::uuid::text, ''), 'UTF8')), 'hex')"
            )
        )
    assert sql == issue_fingerprint("TOTAL_MISMATCH", "line_items[2].amount", related)
    assert none == issue_fingerprint("MISSING_FIELD", None, None)


CANDIDATE_SHAPES = {
    "exact": "e.invoice_number_norm = 'INV1'",
    "fuzzy": "e.total = 270 AND e.invoice_date BETWEEN '2024-03-02' AND '2024-03-08'",
}


@pytest.mark.parametrize("shape", sorted(CANDIDATE_SHAPES))
def test_candidate_queries_never_seq_scan_extractions(sessions, shape: str) -> None:
    """With seq scans priced out (what a large table looks like to the planner),
    both candidate query shapes must be served by the Step 2 indexes."""
    with sessions() as session:
        session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            session.scalars(
                text(
                    "EXPLAIN SELECT e.id FROM extractions e JOIN documents d ON d.id = e.document_id "
                    "WHERE e.is_current AND d.deleted_at IS NULL AND e.supplier_name_norm % 'acme' "
                    f"AND {CANDIDATE_SHAPES[shape]}"
                )
            )
        )
    # The planner picks the most selective Step 2 index (supplier/number btree,
    # the total index, or the trigram GIN index) — any of them, but never a seq scan.
    assert "Seq Scan on extractions" not in plan
    assert "Index" in plan


def test_pg_trgm_agrees_with_python_similarity(sessions) -> None:
    from app.validation.crossdoc import trigram_similarity

    pairs = [("acme supplies", "acme suplies"), ("müller bau", "mueller bau"), ("o reilly sons", "oreilly")]
    with sessions() as session:
        for a, b in pairs:
            assert session.scalar(select(func.similarity(a, b))) == pytest.approx(trigram_similarity(a, b))


def test_pipeline_validate_stage_sets_document_status_and_events(sessions, service, tmp_path) -> None:
    from app.config import PipelineSettings
    from app.extraction.service import ExtractionReport
    from app.models import EventStatus, JobType, ProcessingEvent, ProcessingJob, Stage
    from app.pipeline import orchestrator
    from app.pipeline.storage import LocalStorage

    class NoExtraction:  # extractions are seeded directly; the stage under test is validate
        def extract_document(self, document_id, job_id, *, force=False):
            return ExtractionReport()

    def build(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
        document = add_document(session)
        document.status = DocumentStatus.PROCESSING
        add_invoice(session, document, total="999.00", lines=[])
        job = ProcessingJob(document_id=document.id, job_type=JobType.FULL)
        session.add(job)
        session.flush()
        return document.id, job.id

    document_id, job_id = seed(sessions, build)  # type: ignore[misc]
    ctx = orchestrator.PipelineContext(
        session_factory=sessions,
        storage=LocalStorage(tmp_path),
        ocr_engine_factory=lambda: None,  # type: ignore[arg-type,return-value]
        settings=PipelineSettings(storage_root=tmp_path),
        extractor=NoExtraction(),
        validator=service,
    )
    assert orchestrator.run_llm_stages(ctx, document_id, job_id) is DocumentStatus.NEEDS_REVIEW
    with sessions() as session:
        assert session.get_one(Document, document_id).status is DocumentStatus.NEEDS_REVIEW
        event = session.scalars(
            select(ProcessingEvent).where(
                ProcessingEvent.job_id == job_id, ProcessingEvent.stage == Stage.VALIDATE
            )
        ).one()
    assert event.status is EventStatus.SUCCEEDED
    assert event.details["by_severity"]["error"] == 1 and event.details["by_rule"] == {"TOTAL_MISMATCH": 1}
    assert event.details["document_status"] == "needs_review"
