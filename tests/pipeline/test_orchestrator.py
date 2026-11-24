"""End-to-end pipeline runs against the real test database (committed data)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import (
    Document,
    DocumentStatus,
    DocumentUnit,
    EventStatus,
    ExtractionMethod,
    JobStatus,
    ProcessingEvent,
    ProcessingJob,
    Stage,
)
from app.models.enums import ErrorCode, JobType
from app.pipeline import orchestrator
from app.pipeline.intake import ingest_file
from tests.fixtures import factory
from tests.pipeline.conftest import FailingOcrEngine, FakeOcrEngine


def _ingest(ctx, data: bytes, name: str, job_type: JobType = JobType.FULL):
    with ctx.session() as session:
        doc = ingest_file(
            io.BytesIO(data),
            name,
            "test",
            session,
            storage=ctx.storage,
            enqueue=lambda *_: None,
            settings=ctx.settings,
            job_type=job_type,
        )
        return doc.id, doc.jobs[-1].id


def _new_job(ctx, document_id) -> object:
    with ctx.session() as session:
        job = ProcessingJob(document_id=document_id, job_type=JobType.REPROCESS)
        session.add(job)
        session.flush()
        return job.id


def _events(ctx, document_id, job_id=None):
    with ctx.session() as session:
        q = select(ProcessingEvent).where(ProcessingEvent.document_id == document_id)
        if job_id:
            q = q.where(ProcessingEvent.job_id == job_id)
        return session.scalars(q.order_by(ProcessingEvent.id)).all()


def test_30_page_multi_invoice_pdf_with_one_bad_page(make_context) -> None:
    ctx = make_context(FailingOcrEngine(), page_batch_size=7)  # 5 batches
    data = factory.multi_invoice_pdf(30, pages_per_invoice=3, blank_pages=frozenset({13}))
    doc_id, job_id = _ingest(ctx, data, "multi.pdf")

    assert orchestrator.run_document(ctx, doc_id, job_id) is DocumentStatus.COMPLETED

    with ctx.session() as session:
        doc = session.get_one(Document, doc_id)
        units = session.scalars(
            select(DocumentUnit).where(DocumentUnit.document_id == doc_id).order_by(DocumentUnit.unit_index)
        ).all()
        job = session.get_one(ProcessingJob, job_id)
        assert doc.page_count == 30 and len(units) == 30
        assert doc.processing_started_at <= doc.processing_finished_at
        assert job.status is JobStatus.SUCCEEDED
        bad = units[13]
        assert bad.error_code is ErrorCode.OCR_ERROR and bad.extraction_method is ExtractionMethod.OCR
        assert all(u.error_code is None for u in units if u.unit_index != 13)
        assert len(doc.segments) == 10  # 10 invoices x 3 pages
        assert [(s["unit_start"], s["unit_end"]) for s in doc.segments][:2] == [(0, 2), (3, 5)]

    events = _events(ctx, doc_id, job_id)
    stages = [(e.stage, e.status) for e in events]
    assert stages[0] == (Stage.INGEST, EventStatus.SUCCEEDED)
    assert (Stage.PARSE, EventStatus.STARTED) in stages
    assert sum(1 for e in events if e.details.get("scope") == "batch") == 5
    summary = next(e for e in events if e.details.get("scope") == "document")
    assert summary.details["units"] == 30 and summary.details["failed"] == 1
    assert summary.details["unit_ms"]["digital"]["n"] == 29
    for stage in (Stage.EXTRACT, Stage.VALIDATE, Stage.EMBED):
        assert (stage, EventStatus.SKIPPED) in stages
    assert (Stage.SEGMENT, EventStatus.SUCCEEDED) in stages


def test_rerunning_creates_no_duplicates(make_context) -> None:
    ctx = make_context(FakeOcrEngine())
    doc_id, job_id = _ingest(ctx, factory.multi_invoice_pdf(6), "six.pdf")
    orchestrator.run_document(ctx, doc_id, job_id)
    with ctx.session() as session:
        ids_before = set(session.scalars(select(DocumentUnit.id).where(DocumentUnit.document_id == doc_id)))

    orchestrator.run_document(ctx, doc_id, _new_job(ctx, doc_id))

    with ctx.session() as session:
        ids_after = set(session.scalars(select(DocumentUnit.id).where(DocumentUnit.document_id == doc_id)))
        assert session.get_one(Document, doc_id).status is DocumentStatus.COMPLETED
    assert ids_after == ids_before and len(ids_after) == 6  # upserted in place


def test_too_many_failed_units_fails_document(make_context) -> None:
    ctx = make_context(FailingOcrEngine())
    doc_id, job_id = _ingest(ctx, factory.digital_pdf([[], [], ["x" * 80]]), "mostly_blank.pdf")
    assert orchestrator.run_document(ctx, doc_id, job_id) is DocumentStatus.FAILED
    with ctx.session() as session:
        doc = session.get_one(Document, doc_id)
        job = session.get_one(ProcessingJob, job_id)
        assert doc.error_code is ErrorCode.OCR_ERROR and job.error_code is ErrorCode.OCR_ERROR
        assert doc.error_message == "Text recognition failed for part of the file."


def test_all_units_empty_is_no_text_found(make_context) -> None:
    ctx = make_context(FakeOcrEngine("", None))
    doc_id, job_id = _ingest(ctx, factory.digital_pdf([[], []]), "blank.pdf")
    assert orchestrator.run_document(ctx, doc_id, job_id) is DocumentStatus.FAILED
    with ctx.session() as session:
        assert session.get_one(Document, doc_id).error_code is ErrorCode.NO_TEXT_FOUND


@pytest.mark.parametrize("name", ["truncated.pdf", "repairable.pdf"])
def test_truncated_pdfs_end_corrupt(make_context, fixture_files: dict[str, Path], name: str) -> None:
    """Unrecoverable -> fails at open; repaired-but-empty -> CORRUPT, not NO_TEXT_FOUND."""
    ctx = make_context(FakeOcrEngine("", None))
    doc_id, job_id = _ingest(ctx, fixture_files[name].read_bytes(), name)
    assert orchestrator.run_document(ctx, doc_id, job_id) is DocumentStatus.FAILED
    with ctx.session() as session:
        assert session.get_one(Document, doc_id).error_code is ErrorCode.CORRUPT_FILE


@pytest.mark.parametrize(
    ("name", "code"),
    [("encrypted.pdf", ErrorCode.PASSWORD_PROTECTED), ("bomb.png", ErrorCode.DECOMPRESSION_BOMB)],
)
def test_permanent_file_errors_fail_document_without_units(make_context, fixture_files, name, code) -> None:
    ctx = make_context(FakeOcrEngine())
    doc_id, job_id = _ingest(ctx, fixture_files[name].read_bytes(), name)
    assert orchestrator.run_document(ctx, doc_id, job_id) is DocumentStatus.FAILED
    with ctx.session() as session:
        doc = session.get_one(Document, doc_id)
        assert doc.error_code is code
        assert session.scalar(select(func.count()).where(DocumentUnit.document_id == doc_id)) == 0
    failed = [e for e in _events(ctx, doc_id, job_id) if e.status is EventStatus.FAILED]
    assert failed[0].details["code"] == code.value


@pytest.mark.parametrize("name", ["invoices.xlsx", "invoice.xls", "semicolon_cp1252.csv"])
def test_tabular_documents_complete(make_context, fixture_files, name) -> None:
    ctx = make_context(FakeOcrEngine())
    doc_id, job_id = _ingest(ctx, fixture_files[name].read_bytes(), name)
    assert orchestrator.run_document(ctx, doc_id, job_id) is DocumentStatus.COMPLETED
    with ctx.session() as session:
        doc = session.get_one(Document, doc_id)
        n_units = session.scalar(select(func.count()).where(DocumentUnit.document_id == doc_id))
        assert len(doc.segments) == n_units == doc.page_count


def test_failure_is_idempotent(make_context) -> None:
    from app.pipeline.errors import PermanentError

    ctx = make_context(FakeOcrEngine())
    doc_id, job_id = _ingest(ctx, factory.digital_pdf([["x" * 80]]), "x.pdf")
    err = PermanentError(ErrorCode.INTERNAL_ERROR)
    orchestrator.fail(ctx, doc_id, job_id, err, Stage.PARSE)
    orchestrator.fail(ctx, doc_id, job_id, err, Stage.PARSE)
    assert sum(e.status is EventStatus.FAILED for e in _events(ctx, doc_id, job_id)) == 1
