"""Embed stage: batching, change detection, quota share, deferral, fallback spaces,
summary regeneration, and the pipeline's index stage."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import fakeredis
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.llm.errors import QuotaExhaustedError
from app.models import Chunk, Extraction
from app.models.enums import ChunkKind
from app.search.embedding import ChunkEmbedder, EmbedBudget
from app.search.filters import DocumentFilters
from app.search.indexer import ChunkIndexer
from app.search.keyword import KeywordRetriever
from tests.search.conftest import Corpus
from tests.search.fakes import FALLBACK, PRIMARY, FakeEmbeddingClient, StaticQuota

PAGES = [f"Page {i}: consulting hours for project alpha, line {i}" for i in range(9)]


def _counts(session_factory: sessionmaker[Session]) -> tuple[int, int]:
    with session_factory() as session:
        total = session.scalar(select(func.count()).select_from(Chunk)) or 0
        pending = session.scalar(select(func.count()).where(Chunk.embedding.is_(None))) or 0
    return int(total), int(pending)


def test_chunks_are_written_pending_and_keyword_searchable(
    corpus: Corpus, session_factory: sessionmaker[Session]
) -> None:
    corpus.add("a", PAGES, number="INV-1", supplier="Acme Ltd")
    total, pending = _counts(session_factory)
    assert total == len(PAGES) + 1 and pending == total  # 9 pages + 1 summary, none embedded yet
    with session_factory() as session:
        hits = KeywordRetriever(min_similarity=0.3).fulltext(session, "project alpha", DocumentFilters(), 50)
    assert {h.document_id for h in hits} == {corpus.documents["a"]}


def test_batching(
    corpus: Corpus,
    make_embedder: Callable[..., ChunkEmbedder],
    client: FakeEmbeddingClient,
    session_factory: sessionmaker[Session],
) -> None:
    corpus.add("a", PAGES, number="INV-1")
    report = make_embedder(batch_size=4).embed_pending(limit=100)
    assert (report.embedded, report.requests, report.deferred) == (10, 3, False)
    assert [n for n, *_ in client.calls] == [4, 4, 2]
    assert {t for _, t, _ in client.calls} == {"document"}
    assert _counts(session_factory)[1] == 0
    with session_factory() as session:
        row = session.execute(select(Chunk.embedding_model, Chunk.embedding_space, Chunk.embedded_at)).first()
    assert row is not None and row[0] == f"fake-{PRIMARY}" and row[1] == PRIMARY and row[2] is not None


def test_unchanged_chunks_keep_their_embedding(
    corpus: Corpus,
    make_embedder: Callable[..., ChunkEmbedder],
    client: FakeEmbeddingClient,
    session_factory: sessionmaker[Session],
) -> None:
    corpus.add("a", PAGES, number="INV-1")
    make_embedder().embed_pending(limit=100)
    calls = len(client.calls)
    with session_factory() as session:  # page 3 changes; everything else is identical text
        unit = corpus.unit("a", 3)
        session.merge(unit).text = "Page 3: completely new wording"
        session.commit()
        report = ChunkIndexer(session, corpus.settings).rebuild_document(corpus.documents["a"])
        session.commit()
    assert (report.chunks, report.reused_embeddings, report.pending) == (10, 9, 1)
    assert make_embedder().embed_pending(limit=100).embedded == 1
    assert len(client.calls) == calls + 1


def test_quota_share_is_respected(
    corpus: Corpus,
    make_embedder: Callable[..., ChunkEmbedder],
    client: FakeEmbeddingClient,
    session_factory: sessionmaker[Session],
) -> None:
    corpus.add("a", PAGES, number="INV-1")
    embedder = make_embedder(batch_size=2, limit=10, share=0.3)  # 30% of 10 requests/day = 3 requests
    report = embedder.embed_pending(limit=100)
    assert report.requests == 3 and report.embedded == 6 and report.deferred
    assert report.deferred_until is not None and report.deferred_until > datetime.now(UTC)
    assert len(client.calls) == 3
    assert _counts(session_factory)[1] == 4  # the rest stay pending...
    with session_factory() as session:  # ...and still keyword-searchable
        hits = KeywordRetriever(min_similarity=0.3).fulltext(session, "line 8", DocumentFilters(), 50)
    assert hits
    again = embedder.embed_pending(limit=100)  # same day: nothing more is sent
    assert again.requests == 0 and len(client.calls) == 3


def test_provider_quota_exhaustion_defers(
    corpus: Corpus,
    make_embedder: Callable[..., ChunkEmbedder],
    client: FakeEmbeddingClient,
    session_factory: sessionmaker[Session],
) -> None:
    corpus.add("a", PAGES[:2], number="INV-1")
    resets = datetime(2030, 1, 1, tzinfo=UTC)
    client.fail_with = QuotaExhaustedError(resets)
    report = make_embedder().embed_pending(limit=100)
    assert report.deferred_until == resets and report.embedded == 0
    assert _counts(session_factory)[1] == 3


def test_fallback_space_is_tagged(
    corpus: Corpus,
    make_embedder: Callable[..., ChunkEmbedder],
    client: FakeEmbeddingClient,
    session_factory: sessionmaker[Session],
) -> None:
    corpus.add("a", PAGES[:2], number="INV-1")
    client.down = {PRIMARY}
    report = make_embedder().embed_pending(limit=100)
    assert report.spaces == (FALLBACK,)
    with session_factory() as session:
        spaces = set(session.scalars(select(Chunk.embedding_space)))
    assert spaces == {FALLBACK}


def test_summary_chunk_is_regenerated_after_a_correction(
    corpus: Corpus, make_embedder: Callable[..., ChunkEmbedder], session_factory: sessionmaker[Session]
) -> None:
    corpus.add("a", PAGES[:1], number="INV-1", supplier="Acme Ltd", customer="Beta Corp")
    make_embedder().embed_pending(limit=100)
    with session_factory() as session:
        extraction = session.get_one(Extraction, corpus.extractions["a"])
        extraction.customer_name = "Gamma GmbH"
        session.flush()
        ChunkIndexer(session, corpus.settings).rebuild_summary(extraction.id)
        session.commit()
        summary = session.scalars(select(Chunk).where(Chunk.chunk_kind == ChunkKind.EXTRACTION_SUMMARY)).one()
        content_chunks = session.scalars(select(Chunk).where(Chunk.chunk_kind == ChunkKind.CONTENT)).all()
    assert "to Gamma GmbH" in summary.content and "Beta" not in summary.content
    assert summary.embedding is None  # new text: back in the queue
    assert all(c.embedding is not None for c in content_chunks)  # untouched


def test_budget_counts_per_day() -> None:
    days = iter([datetime(2026, 3, 1, tzinfo=UTC)] * 3 + [datetime(2026, 3, 2, tzinfo=UTC)] * 2)
    budget = EmbedBudget(fakeredis.FakeRedis(), StaticQuota(5), share=0.4, now=lambda: next(days))  # 2/day
    assert [budget.try_take(), budget.try_take(), budget.try_take()] == [True, True, False]
    assert budget.try_take()  # a new UTC day
    assert budget.allowance() == 2
