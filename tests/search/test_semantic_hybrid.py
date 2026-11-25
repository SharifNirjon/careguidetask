"""Semantic search (ranking, space isolation, filters, query cache, EXPLAIN),
RRF fusion, degradation, and re-embedding between spaces."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import fakeredis
import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.llm.errors import LLMUnavailableError, QuotaExhaustedError
from app.models import Chunk
from app.models.search import hnsw_index_name
from app.search.embedding import ChunkEmbedder
from app.search.filters import DocumentFilters
from app.search.fusion import normalized, rrf
from app.search.reembed import Reembedder
from app.search.semantic import QueryEmbedder, QueryEmbeddingUnavailable, SemanticRetriever, similarity
from app.search.service import HybridSearchService
from app.search.types import MatchType, SearchMode, SearchQuery
from tests.search.conftest import Corpus
from tests.search.fakes import FALLBACK, PRIMARY, FakeEmbeddingClient, bag_of_words

ANY = DocumentFilters()


@pytest.fixture
def library(corpus: Corpus, make_embedder: Callable[..., ChunkEmbedder]) -> Corpus:
    corpus.add(
        "chairs",
        ["Ergonomic office chairs with adjustable lumbar support"],
        supplier="Acme Ltd",
        total="900.00",
        currency="EUR",
    )
    corpus.add(
        "laptops",
        ["Laptop computers with docking stations and monitors"],
        supplier="Globex Corp",
        total="2400.00",
        currency="USD",
    )
    corpus.add(
        "catering",
        ["Catering lunch buffet for the quarterly meeting"],
        supplier="Initech",
        total="300.00",
        currency="EUR",
    )
    make_embedder().embed_pending(limit=100)
    return corpus


def _search(
    session_factory: sessionmaker[Session],
    service: HybridSearchService,
    q: str,
    mode: SearchMode,
    filters: DocumentFilters = ANY,
) -> list[str]:
    with session_factory() as session:
        result = service.search(session, SearchQuery(q, mode, filters, 10))
    assert not result.degraded, result.degraded_reason
    return [h.filename.removesuffix(".pdf") for h in result.hits]


def test_relevant_chunk_ranks_first(
    library: Corpus, session_factory: sessionmaker[Session], make_service: Callable[..., HybridSearchService]
) -> None:
    assert (
        _search(session_factory, make_service(), "adjustable office chairs", SearchMode.SEMANTIC)[0]
        == "chairs"
    )
    assert (
        _search(session_factory, make_service(), "monitors and laptop", SearchMode.SEMANTIC)[0] == "laptops"
    )


def test_similarity_is_mapped_to_unit_interval() -> None:
    assert (similarity(0.0), similarity(1.0), similarity(2.0), similarity(2.5)) == (1.0, 0.5, 0.0, 0.0)


def test_semantic_filters_apply(
    library: Corpus, session_factory: sessionmaker[Session], make_service: Callable[..., HybridSearchService]
) -> None:
    eur_only = DocumentFilters(currency="EUR")
    names = _search(session_factory, make_service(), "laptop computers", SearchMode.SEMANTIC, eur_only)
    assert "laptops" not in names and names  # the best match is filtered out, others remain
    assert _search(
        session_factory, make_service(), "chairs", SearchMode.SEMANTIC, DocumentFilters(supplier="globex")
    ) == ["laptops"]


def test_space_isolation(
    library: Corpus,
    session_factory: sessionmaker[Session],
    client: FakeEmbeddingClient,
    make_embedder: Callable[..., ChunkEmbedder],
) -> None:
    client.down = {PRIMARY}
    library.add("fallback_doc", ["Ergonomic office chairs, identical wording to the chairs invoice"])
    make_embedder().embed_pending(limit=100)
    client.down = set()
    retriever = SemanticRetriever(ef_search=100)
    with session_factory() as session:
        assert SemanticRetriever.spaces_with_embeddings(session) == [PRIMARY, FALLBACK]
        primary = retriever.search(session, bag_of_words("office chairs", PRIMARY), PRIMARY, ANY, 50)
        fallback = retriever.search(session, bag_of_words("office chairs", FALLBACK), FALLBACK, ANY, 50)
        spaces = dict(session.execute(select(Chunk.id, Chunk.embedding_space)).all())
    assert {spaces[h.chunk_id] for h in primary.hits} == {PRIMARY}
    assert {spaces[h.chunk_id] for h in fallback.hits} == {FALLBACK}
    assert library.documents["fallback_doc"] in {h.document_id for h in fallback.hits}
    assert library.documents["fallback_doc"] not in {h.document_id for h in primary.hits}


def test_query_embedding_cache(
    redis: fakeredis.FakeRedis, client: FakeEmbeddingClient, make_budget: Callable[..., object]
) -> None:
    embedder = QueryEmbedder(client=client, redis=redis, budget=make_budget(), ttl_s=60)  # type: ignore[arg-type]
    first = embedder.embed("Office  CHAIRS", PRIMARY)
    second = embedder.embed("office chairs", PRIMARY)  # normalized: same cache entry
    assert first == second and len(client.calls) == 1
    assert client.calls[0][1] == "query"
    assert "office" not in next(iter(redis.keys("search:qvec:*"))).decode()  # the text is hashed
    embedder.embed("office chairs", FALLBACK)  # another space is another vector
    assert len(client.calls) == 2


def test_query_embedding_respects_the_share(
    redis: fakeredis.FakeRedis, client: FakeEmbeddingClient, make_budget: Callable[..., object]
) -> None:
    embedder = QueryEmbedder(
        client=client,
        redis=redis,
        budget=make_budget(limit=3, share=0.34),  # type: ignore[arg-type]
        ttl_s=60,
    )
    embedder.embed("one", PRIMARY)
    with pytest.raises(QueryEmbeddingUnavailable, match="share"):
        embedder.embed("two", PRIMARY)
    assert len(client.calls) == 1


def test_explain_uses_the_partial_hnsw_index(library: Corpus, session_factory: sessionmaker[Session]) -> None:
    """The real query (with filters and joins), compiled and EXPLAINed."""
    retriever = SemanticRetriever(ef_search=100)
    captured: list[tuple[str, Any]] = []

    def capture(
        conn: object, cursor: object, statement: str, params: Any, context: object, many: bool
    ) -> None:
        if "<=>" in statement:
            captured.append((statement, params))

    with session_factory() as session:
        session.execute(
            text("SET LOCAL enable_seqscan = off")
        )  # tiny table: make the planner show its options
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            retriever.search(session, bag_of_words("chairs"), PRIMARY, DocumentFilters(currency="EUR"), 5)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        statement, params = captured[0]
        rows = session.connection().exec_driver_sql("EXPLAIN " + statement, params).all()
    plan = "\n".join(r[0] for r in rows)
    assert f"'{PRIMARY}'" in statement  # space inlined as a literal: matches the partial index predicate
    assert hnsw_index_name(PRIMARY) in plan, plan


# --------------------------------------------------------------------- hybrid


def test_rrf_math() -> None:
    scores = rrf([["a", "b", "c"], ["b", "a"], ["c", "c", "a"]], k=60)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)  # duplicate "c" counts once, at its best rank
    assert max(scores, key=scores.__getitem__) == "a"
    assert normalized(2 / 61, n_lists=2, k=60) == 1.0 and normalized(1 / 61, n_lists=2, k=60) == 0.5


def test_hybrid_labels_match_types(
    library: Corpus, session_factory: sessionmaker[Session], make_service: Callable[..., HybridSearchService]
) -> None:
    with session_factory() as session:
        result = make_service().search(session, SearchQuery("lunch buffet", SearchMode.HYBRID, ANY, 10))
    top = result.hits[0]
    assert top.filename == "catering.pdf" and top.match_type is MatchType.BOTH
    assert {h.match_type for h in result.hits[1:]} <= {MatchType.SEMANTIC}  # no keyword hits elsewhere
    assert {"keyword", "embed", "vector", "fuse"} <= set(result.timings_ms)


@pytest.mark.parametrize(
    "failure", [LLMUnavailableError({"m": "down"}), QuotaExhaustedError(datetime(2030, 1, 1))]
)
def test_degraded_mode_when_embedding_fails(
    library: Corpus,
    session_factory: sessionmaker[Session],
    make_service: Callable[..., HybridSearchService],
    client: FakeEmbeddingClient,
    failure: Exception,
) -> None:
    client.fail_with = failure  # type: ignore[assignment]
    for mode in (SearchMode.SEMANTIC, SearchMode.HYBRID):
        with session_factory() as session:
            result = make_service().search(session, SearchQuery("lunch buffet", mode, ANY, 10))
        assert result.degraded and result.degraded_reason and "keyword" in result.degraded_reason
        assert [h.filename for h in result.hits] == ["catering.pdf"]
        assert result.hits[0].match_type is MatchType.KEYWORD


def test_no_embedder_configured_degrades(
    library: Corpus, session_factory: sessionmaker[Session], make_service: Callable[..., HybridSearchService]
) -> None:
    with session_factory() as session:
        result = make_service(embedder=False).search(
            session, SearchQuery("buffet", SearchMode.HYBRID, ANY, 10)
        )
    assert result.degraded and result.hits


# -------------------------------------------------------------------- reembed


def test_reembed_moves_fallback_chunks_to_primary(
    library: Corpus,
    session_factory: sessionmaker[Session],
    client: FakeEmbeddingClient,
    make_embedder: Callable[..., ChunkEmbedder],
) -> None:
    with (
        session_factory() as session,
        session.begin(),
    ):  # pretend these were embedded while the primary was down
        session.execute(
            update(Chunk)
            .where(Chunk.document_id == library.documents["laptops"])
            .values(embedding_space=FALLBACK, embedding_model="fake-fallback")
        )
    healthy = {"value": False}
    reembedder = Reembedder(
        session_factory=session_factory,
        embedder=make_embedder(),
        primary_space=PRIMARY,
        primary_healthy=lambda: healthy["value"],
    )
    assert reembedder.run(from_spaces=[FALLBACK], limit=100).skipped_reason == (
        "primary embedding model unavailable (circuit open)"
    )
    healthy["value"] = True
    report = reembedder.run(from_spaces=[FALLBACK], limit=100)
    assert report.selected == report.moved == 2
    with session_factory() as session:
        assert set(session.scalars(select(Chunk.embedding_space))) == {PRIMARY}
    again = reembedder.run(from_spaces=[FALLBACK], limit=100)  # idempotent: nothing left to move
    assert (again.selected, again.moved) == (0, 0)
    assert {space for *_, space in client.calls[-1:]} == {PRIMARY}
