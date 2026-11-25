"""ExtractionService + orchestrator extract stage, on the real test database.

Documents go through the real Step 3 pipeline (ingest -> parse -> segment);
the chat gateway is a deterministic fake that answers from the prompt text.
"""

from __future__ import annotations

import io
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from app.config import ExtractionSettings, PipelineSettings
from app.extraction.prompts import load_prompt
from app.extraction.schemas import InvoiceExtractionLLM
from app.extraction.service import ExtractionService
from app.llm import (
    ChatResult,
    ExtractionParseError,
    LLMUnavailableError,
    OutputMode,
    QuotaExhaustedError,
    TokenUsage,
)
from app.llm.clock import SystemClock
from app.models import (
    Document,
    DocumentStatus,
    EventStatus,
    Extraction,
    JobStatus,
    ProcessingEvent,
    ProcessingJob,
    Stage,
)
from app.models.enums import ErrorCode, JobType
from app.pipeline import orchestrator
from app.pipeline.errors import DeferredError, TransientError
from app.pipeline.intake import ingest_file
from app.pipeline.storage import LocalStorage
from tests.fixtures import factory
from tests.pipeline.conftest import FakeOcrEngine
from tests.support.extraction import llm_result

Responder = Callable[[str], InvoiceExtractionLLM]
_UNIT = re.compile(r"=== UNIT (\d+) ")
_NUMBER = re.compile(r"Invoice Number: (INV-\d+)")


class FakeChat:
    """StructuredChat double: answers are built from the prompt it receives."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.calls: list[str] = []

    def complete_structured(self, messages, response_model, *, purpose, prompt_version, force=False):
        user = messages[-1].content
        self.calls.append(user)
        parsed = self.responder(user)
        return ChatResult(
            parsed=parsed,
            raw_text=parsed.model_dump_json(),
            model_used="fake/model:free",
            usage=TokenUsage(120, 40, 0),
            latency_ms=5,
            attempts=1,
            cached=False,
            output_mode=OutputMode.PROMPT,
            reasoning_effort="medium",
            repaired=False,
        )


def grounded_answer(user: str) -> InvoiceExtractionLLM:
    """Quote evidence exactly from the units in the prompt, like a well-behaved model."""
    units = dict(
        zip((int(i) for i in _UNIT.findall(user)), re.split(r"=== UNIT \d+ .*===\n", user)[1:], strict=True)
    )
    number_unit = next(i for i, text in units.items() if _NUMBER.search(text))
    number = _NUMBER.search(units[number_unit]).group(1)  # type: ignore[union-attr]
    total_unit = next((i for i, t in units.items() if "Total Amount Due 270.00 EUR" in t), None)
    total = ("270.00", "Total Amount Due 270.00 EUR", total_unit) if total_unit is not None else None
    return llm_result(
        invoice_number=(number, f"Invoice Number: {number}", number_unit),
        supplier_name=("ACME Supplies Ltd", "Supplier: ACME Supplies Ltd", number_unit),
        total=total,
        currency=("EUR", "270.00 EUR", total_unit) if total else None,
    )


@pytest.fixture
def build(committed_engine: Engine, tmp_path: Path):
    def make(responder: Responder) -> tuple[orchestrator.PipelineContext, FakeChat]:
        chat = FakeChat(responder)
        sessions = sessionmaker(bind=committed_engine, expire_on_commit=False)
        service = ExtractionService(
            session_factory=sessions,
            chat=chat,
            prompt=load_prompt(),
            settings=ExtractionSettings(),
            clock=SystemClock(),
        )
        ctx = orchestrator.PipelineContext(
            session_factory=sessions,
            storage=LocalStorage(tmp_path / "storage"),
            ocr_engine_factory=FakeOcrEngine,
            settings=PipelineSettings(storage_root=tmp_path / "storage"),
            extractor=service,
        )
        return ctx, chat

    return make


def ingest_and_run(ctx: orchestrator.PipelineContext, data: bytes, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    with ctx.session() as session:
        doc = ingest_file(
            io.BytesIO(data),
            name,
            "test",
            session,
            storage=ctx.storage,
            enqueue=lambda *_: None,
            settings=ctx.settings,
        )
        ids = doc.id, doc.jobs[0].id
    orchestrator.run_document(ctx, *ids)
    return ids


def new_job(ctx: orchestrator.PipelineContext, document_id: uuid.UUID) -> uuid.UUID:
    with ctx.session() as session:
        job = ProcessingJob(document_id=document_id, job_type=JobType.REEXTRACT)
        session.add(job)
        session.flush()
        return job.id


def extractions(ctx: orchestrator.PipelineContext, document_id: uuid.UUID) -> list[Extraction]:
    with ctx.session() as session:
        return list(
            session.scalars(
                select(Extraction)
                .where(Extraction.document_id == document_id)
                .order_by(Extraction.invoice_index, Extraction.version)
            )
        )


NINE_PAGES = factory.multi_invoice_pdf(9, pages_per_invoice=3)


def test_multi_segment_pdf_gives_one_versioned_extraction_per_segment(build) -> None:
    ctx, chat = build(grounded_answer)
    doc_id, job_id = ingest_and_run(ctx, NINE_PAGES, "nine.pdf")
    rows = extractions(ctx, doc_id)
    assert len(chat.calls) == 3
    assert [(r.invoice_index, r.version, r.is_current) for r in rows] == [
        (0, 1, True),
        (1, 1, True),
        (2, 1, True),
    ]
    assert [r.invoice_number for r in rows] == ["INV-1000", "INV-1001", "INV-1002"]
    first = rows[0]
    assert (first.unit_start, first.unit_end) == (0, 2)
    assert first.total == Decimal("270.0000") and first.currency == "EUR"
    assert first.supplier_name_norm == "acme supplies" and first.invoice_number_norm == "INV1000"
    assert first.processing_job_id == job_id and first.model_name == "fake/model:free"
    assert first.prompt_version == "invoice_extraction_v1"
    assert first.token_usage["prompt_tokens"] == 120 and first.raw_llm_response["parts"][0]["scope"] == "full"
    assert first.field_meta["invoice_number"]["evidence_text"] == "Invoice Number: INV-1000"
    assert first.field_meta["total"]["grounded"] is True
    with ctx.session() as session:
        assert session.get_one(Document, doc_id).status is DocumentStatus.COMPLETED
        events = session.scalars(
            select(ProcessingEvent).where(
                ProcessingEvent.job_id == job_id, ProcessingEvent.stage == Stage.EXTRACT
            )
        ).all()
    segments = [e for e in events if e.details.get("scope") == "segment"]
    assert len(segments) == 3 and segments[0].details["model"] == "fake/model:free"
    assert segments[0].details["usage"]["prompt_tokens"] == 120 and segments[0].duration_ms is not None
    summary = next(e for e in events if e.details.get("scope") == "document")
    assert summary.details["llm_requests"] == 3 and summary.details["cached_parts"] == 0


def test_re_extraction_creates_version_2(build) -> None:
    ctx, _ = build(grounded_answer)
    doc_id, _ = ingest_and_run(ctx, NINE_PAGES, "nine.pdf")
    orchestrator.run_llm_stages(ctx, doc_id, new_job(ctx, doc_id))
    rows = extractions(ctx, doc_id)
    assert [(r.invoice_index, r.version, r.is_current) for r in rows] == [
        (0, 1, False),
        (0, 2, True),
        (1, 1, False),
        (1, 2, True),
        (2, 1, False),
        (2, 2, True),
    ]


def test_retrying_the_same_job_creates_nothing_and_spends_nothing(build) -> None:
    ctx, chat = build(grounded_answer)
    doc_id, job_id = ingest_and_run(ctx, NINE_PAGES, "nine.pdf")
    calls = len(chat.calls)
    orchestrator.run_llm_stages(ctx, doc_id, job_id)  # e.g. a Celery redelivery
    assert len(extractions(ctx, doc_id)) == 3
    assert len(chat.calls) == calls  # no quota spent on work already persisted


def test_one_failing_segment_does_not_block_the_rest(build) -> None:
    def flaky(user: str) -> InvoiceExtractionLLM:
        if "INV-1001" in user:
            raise ExtractionParseError("total: field required", "{}")
        return grounded_answer(user)

    ctx, _ = build(flaky)
    doc_id, _ = ingest_and_run(ctx, NINE_PAGES, "nine.pdf")
    rows = extractions(ctx, doc_id)
    assert [r.invoice_number for r in rows] == ["INV-1000", None, "INV-1002"]
    assert rows[1].field_meta["_error"]["code"] == ErrorCode.LLM_ERROR.value
    assert rows[1].total is None and rows[1].is_current
    with ctx.session() as session:
        assert session.get_one(Document, doc_id).status is DocumentStatus.COMPLETED


def test_quota_exhaustion_defers_instead_of_failing(build) -> None:
    resets = datetime(2026, 3, 2, tzinfo=UTC)

    def spent(_: str) -> InvoiceExtractionLLM:
        raise QuotaExhaustedError(resets)

    ctx, _ = build(spent)
    doc_id, job_id = ingest_and_run(ctx, NINE_PAGES, "nine.pdf")  # sync run returns QUEUED
    with ctx.session() as session:
        doc = session.get_one(Document, doc_id)
        job = session.get_one(ProcessingJob, job_id)
        assert doc.status is DocumentStatus.QUEUED and doc.error_code is ErrorCode.LLM_QUOTA_DEFERRED
        assert job.status is JobStatus.QUEUED
        deferral = session.scalars(
            select(ProcessingEvent).where(
                ProcessingEvent.job_id == job_id, ProcessingEvent.stage == Stage.EXTRACT
            )
        ).one()
    assert deferral.status is EventStatus.SKIPPED and deferral.details["deferred_until"] == resets.isoformat()
    assert extractions(ctx, doc_id) == []
    with pytest.raises(DeferredError):  # the Celery task re-schedules on this
        orchestrator.run_llm_stages(ctx, doc_id, job_id)


def test_all_models_down_is_transient(build) -> None:
    def down(_: str) -> InvoiceExtractionLLM:
        raise LLMUnavailableError({"fake/model:free": "503"})

    ctx, _ = build(down)
    doc_id, _ = ingest_and_run(ctx, NINE_PAGES, "nine.pdf")
    with pytest.raises(TransientError) as info:
        orchestrator.run_llm_stages(ctx, doc_id, new_job(ctx, doc_id))
    assert info.value.code is ErrorCode.LLM_UNAVAILABLE


def _xlsx(rows: list[list[str]]) -> bytes:
    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_clean_spreadsheet_costs_zero_llm_requests(build) -> None:
    ctx, chat = build(grounded_answer)
    data = _xlsx(
        [
            ["Invoice No", "Supplier", "Total", "Currency"],
            ["A-1", "ACME", "10.00", "EUR"],
            ["A-2", "ACME", "20.00", "EUR"],
            ["A-3", "Beta", "30.00", "GBP"],
        ]
    )
    doc_id, _ = ingest_and_run(ctx, data, "register.xlsx")
    rows = extractions(ctx, doc_id)
    assert chat.calls == []
    assert [(r.invoice_index, r.invoice_number, r.total) for r in rows] == [
        (0, "A-1", Decimal("10.0000")),
        (1, "A-2", Decimal("20.0000")),
        (2, "A-3", Decimal("30.0000")),
    ]
    assert rows[0].model_name == "rule:tabular_v1" and rows[0].prompt_version == "tabular_v1"
    assert rows[0].field_meta["total"]["evidence_text"] == "sheet Sheet row 2 col Total"


def test_auxiliary_sheets_next_to_an_invoice_register_cost_nothing(build) -> None:
    ctx, chat = build(grounded_answer)
    wb = openpyxl.Workbook()
    wb.active.title = "Register"
    wb.active.append(["Invoice No", "Total"])
    wb.active.append(["A-1", "10.00"])
    lookup = wb.create_sheet("Rates")
    lookup.append(["Code", "Rate"])
    lookup.append(["EUR", "1.0"])
    buf = io.BytesIO()
    wb.save(buf)
    doc_id, job_id = ingest_and_run(ctx, buf.getvalue(), "book.xlsx")
    assert chat.calls == []
    assert [r.invoice_number for r in extractions(ctx, doc_id)] == ["A-1"]
    with ctx.session() as session:
        statuses = [
            e.details.get("status")
            for e in session.scalars(
                select(ProcessingEvent).where(
                    ProcessingEvent.job_id == job_id, ProcessingEvent.stage == Stage.EXTRACT
                )
            )
        ]
    assert "auxiliary_sheet" in statuses


def test_unrecognized_sheet_falls_back_to_the_llm(build) -> None:
    ctx, chat = build(lambda _: llm_result(document_type="other"))
    doc_id, _ = ingest_and_run(ctx, _xlsx([["Ref", "Who"], ["x1", "Bob"]]), "odd.xlsx")
    assert len(chat.calls) == 1 and "Ref | Who" in chat.calls[0]
    row = extractions(ctx, doc_id)[0]
    assert row.field_meta["_document_type"] == "other"
    assert "NOT_AN_INVOICE" in row.field_meta["_uncertainties"]
