"""The pipeline's index stage: a processed document is keyword-searchable before
any embedding exists, and embedding is scheduled only after the final commit."""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.config import SearchSettings
from app.models import Chunk, Document, DocumentStatus, ProcessingEvent, Stage
from app.models.enums import EventStatus
from app.pipeline import orchestrator
from app.pipeline.intake import ingest_file
from app.pipeline.orchestrator import PipelineContext
from app.search.filters import DocumentFilters
from app.search.keyword import KeywordRetriever
from tests.fixtures import factory


def _process(ctx: PipelineContext, data: bytes) -> uuid.UUID:
    with ctx.session() as session:
        doc = ingest_file(
            io.BytesIO(data),
            "invoice.pdf",
            "test",
            session,
            storage=ctx.storage,
            enqueue=lambda *_: None,
            settings=ctx.settings,
        )
        document_id, job_id = doc.id, doc.jobs[0].id
    orchestrator.run_document(ctx, document_id, job_id)
    return document_id


def test_document_is_searchable_before_embeddings(make_context: Callable[..., PipelineContext]) -> None:
    scheduled: list[uuid.UUID] = []
    ctx = replace(make_context(), search=SearchSettings(), schedule_embedding=scheduled.append)
    document_id = _process(ctx, factory.digital_pdf([factory.invoice_lines("INV-2026-777")]))

    with ctx.session() as session:
        assert session.get_one(Document, document_id).status is DocumentStatus.COMPLETED  # didn't wait
        chunks = session.scalars(select(Chunk).where(Chunk.document_id == document_id)).all()
        assert chunks and all(c.embedding is None for c in chunks)
        hits = KeywordRetriever(min_similarity=0.3).fulltext(session, "INV-2026-777", DocumentFilters(), 10)
        assert {h.document_id for h in hits} == {document_id}
        event = session.scalars(
            select(ProcessingEvent).where(
                ProcessingEvent.document_id == document_id, ProcessingEvent.stage == Stage.EMBED
            )
        ).one()
        assert event.status is EventStatus.SUCCEEDED and event.details["pending"] == len(chunks)
    assert scheduled == [document_id]


def test_indexing_failure_never_fails_the_document(
    make_context: Callable[..., PipelineContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("index bug")

    monkeypatch.setattr(orchestrator.ChunkIndexer, "rebuild_document", broken)
    ctx = replace(make_context(), search=SearchSettings())
    document_id = _process(ctx, factory.digital_pdf([factory.invoice_lines("INV-2026-778")]))
    with ctx.session() as session:
        assert session.get_one(Document, document_id).status is DocumentStatus.COMPLETED
        event = session.scalars(
            select(ProcessingEvent).where(
                ProcessingEvent.document_id == document_id, ProcessingEvent.stage == Stage.EMBED
            )
        ).one()
        assert event.status is EventStatus.FAILED and event.details == {"error_type": "RuntimeError"}
