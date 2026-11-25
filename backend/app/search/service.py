"""HybridSearchService: keyword, semantic and hybrid search behind one protocol.

Lists that are fused (all reduced to result keys first, see fusion.py):
  keyword   - full text over chunks, and field matches on extractions
  semantic  - one k-NN list per embedding space that has matching chunks
Mode keyword uses the keyword lists, semantic the semantic lists, hybrid all.

Degradation: if the query can't be embedded (quota share spent, provider
quota, outage, open circuit) or nothing is embedded yet, semantic and hybrid
answer with keyword results and degraded=true plus a reason. Never a 500.

Logged per query: mode, phase latencies, result count, degraded. The query
text is never logged at INFO (it can contain names and amounts).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from app.config import SearchSettings
from app.models import Chunk, Document, Extraction
from app.search.fusion import Candidate, ResultKey, normalized, rrf
from app.search.keyword import (
    SIMPLE,
    ChunkHit,
    FieldHit,
    KeywordRetriever,
    looks_like_invoice_number,
    prepare_websearch,
)
from app.search.semantic import QueryEmbedder, QueryEmbeddingUnavailable, SemanticRetriever, SpaceHits
from app.search.snippets import (
    HEADLINE_OPTIONS,
    LESS_THAN,
    START,
    STOP,
    field_snippet,
    keyword_snippet,
    semantic_snippet,
)
from app.search.types import SearchHit, SearchMode, SearchQuery, SearchResult, Segment

log = logging.getLogger(__name__)

MS_PER_S: Final = 1000
FIELD_LABELS: Final = {"invoice_number": "Invoice", "supplier_name": "Supplier", "customer_name": "Customer"}
NOT_CONFIGURED: Final = "semantic search is not configured"
NOTHING_EMBEDDED: Final = "no embedded chunks match yet; showing keyword results"


class _Timer:
    def __init__(self) -> None:
        self.ms: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.ms[name] = round(self.ms.get(name, 0.0) + (time.perf_counter() - started) * MS_PER_S, 2)


class HybridSearchService:
    def __init__(
        self, *, settings: SearchSettings, min_similarity: float, query_embedder: QueryEmbedder | None
    ) -> None:
        self._settings = settings
        self._keyword = KeywordRetriever(min_similarity=min_similarity)
        self._semantic = SemanticRetriever(ef_search=settings.search_ef)
        self._embedder = query_embedder

    def search(self, session: Session, query: SearchQuery) -> SearchResult:
        timer = _Timer()
        text = query.text.strip()
        depth = self._settings.candidates_per_list
        semantic: list[SpaceHits] = []
        reason: str | None = None
        if query.mode is not SearchMode.KEYWORD:
            semantic, reason = self._semantic_lists(session, text, query, timer, depth)
        fulltext: list[ChunkHit] = []
        fields: list[FieldHit] = []
        if query.mode is not SearchMode.SEMANTIC or reason is not None:
            with timer.phase("keyword"):
                fulltext = self._keyword.fulltext(session, text, query.filters, depth)
                fields = self._keyword.fields(session, text, query.filters, depth)
        with timer.phase("fuse"):
            ranked = self._fuse(text, fulltext, fields, semantic)
        capped = ranked[: self._settings.max_search_results]
        page = capped[query.offset : query.offset + query.limit]
        with timer.phase("hydrate"):
            hits = self._hydrate(session, page, text)
        log.info(
            "search",
            extra={
                "mode": query.mode.value,
                "results": len(capped),
                "page": len(hits),
                "degraded": reason is not None,
                "spaces": [s.space for s in semantic],
                "timings_ms": timer.ms,
            },
        )
        return SearchResult(
            hits=hits,
            total=len(capped),
            degraded=reason is not None,
            degraded_reason=reason,
            timings_ms=timer.ms,
        )

    # ------------------------------------------------------------ phases

    def _semantic_lists(
        self, session: Session, text: str, query: SearchQuery, timer: _Timer, depth: int
    ) -> tuple[list[SpaceHits], str | None]:
        if self._embedder is None:
            return [], NOT_CONFIGURED
        with timer.phase("spaces"):
            spaces = SemanticRetriever.spaces_with_embeddings(session)
        if not spaces:
            return [], NOTHING_EMBEDDED
        lists = []
        for space in spaces:
            try:
                with timer.phase("embed"):
                    vector = self._embedder.embed(text, space)
            except QueryEmbeddingUnavailable as exc:
                return [], f"{exc.reason}; showing keyword results"
            with timer.phase("vector"):
                lists.append(self._semantic.search(session, vector, space, query.filters, depth))
        return lists, None

    def _fuse(
        self, text: str, fulltext: list[ChunkHit], fields: list[FieldHit], semantic: list[SpaceHits]
    ) -> list[Candidate]:
        k = self._settings.rrf_k
        keyword_lists: list[list[ResultKey]] = [
            [(h.document_id, h.extraction_id) for h in fulltext],
            [(h.document_id, h.extraction_id) for h in fields],
        ]
        semantic_lists: list[list[ResultKey]] = [
            [(h.document_id, h.extraction_id) for h in s.hits] for s in semantic
        ]
        used = [lst for lst in (*keyword_lists, *semantic_lists) if lst]
        scores = rrf(used, k=k)
        candidates: dict[ResultKey, Candidate] = {key: Candidate(key, score) for key, score in scores.items()}
        for hit in fulltext:
            c = candidates[(hit.document_id, hit.extraction_id)]
            if not c.keyword:
                c.keyword, c.keyword_chunk, c.unit_index = True, hit.chunk_id, hit.unit_index
        number_query = looks_like_invoice_number(text)
        for f in fields:
            c = candidates[(f.document_id, f.extraction_id)]
            c.keyword = True
            c.field_match = c.field_match or f.matched_field
            c.unit_index = c.unit_index if c.unit_index is not None else f.unit_index
            if f.exact_number and number_query and not c.exact_number:
                c.exact_number = True
                c.score += self._settings.invoice_number_boost  # an exact number beats any fuzzy match
        for space_hits in semantic:
            for hit in space_hits.hits:
                c = candidates[(hit.document_id, hit.extraction_id)]
                if not c.semantic:
                    c.semantic, c.semantic_chunk = True, hit.chunk_id
                    c.unit_index = c.unit_index if c.unit_index is not None else hit.unit_index
        ordered = sorted(candidates.values(), key=lambda c: (-c.score, str(c.key)))
        for c in ordered:
            c.score = 1.0 if c.exact_number else normalized(c.score, len(used), k)
        return ordered

    def _hydrate(self, session: Session, page: list[Candidate], text: str) -> list[SearchHit]:
        if not page:
            return []
        extraction_ids = [c.key[1] for c in page if c.key[1] is not None]
        extractions = {
            e.id: e for e in session.scalars(select(Extraction).where(Extraction.id.in_(extraction_ids)))
        }
        documents = {
            d.id: d
            for d in session.scalars(select(Document).where(Document.id.in_({c.key[0] for c in page})))
        }
        keyword_chunks = [c.keyword_chunk for c in page if c.keyword_chunk]
        semantic_chunks = [c.semantic_chunk for c in page if c.semantic_chunk and not c.keyword_chunk]
        headlines = self._headlines(session, keyword_chunks, text)
        contents = dict(
            session.execute(
                select(Chunk.id, Chunk.content).where(Chunk.id.in_([*keyword_chunks, *semantic_chunks]))
            ).all()
        )
        return [
            self._hit(
                c,
                documents[c.key[0]],
                extractions.get(c.key[1]) if c.key[1] else None,
                headlines,
                contents,
                text,
            )
            for c in page
        ]

    @staticmethod
    def _headlines(session: Session, chunk_ids: list[uuid.UUID], text: str) -> dict[uuid.UUID, str]:
        """ts_headline only for the page (it re-parses every chunk it is given)."""
        if not chunk_ids:
            return {}
        query = func.websearch_to_tsquery(SIMPLE, text).op("||")(
            func.websearch_to_tsquery(SIMPLE, prepare_websearch(text))
        )
        clean = func.replace(func.replace(func.replace(Chunk.content, START, ""), STOP, ""), "<", LESS_THAN)
        rows = session.execute(
            select(Chunk.id, func.ts_headline(SIMPLE, clean, query, literal(HEADLINE_OPTIONS))).where(
                Chunk.id.in_(chunk_ids)
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    @staticmethod
    def _hit(
        c: Candidate,
        document: Document,
        extraction: Extraction | None,
        headlines: dict[uuid.UUID, str],
        contents: dict[uuid.UUID, str],
        text: str,
    ) -> SearchHit:
        snippet: tuple[Segment, ...] = ()
        if c.keyword_chunk:
            snippet = keyword_snippet(headlines.get(c.keyword_chunk), contents.get(c.keyword_chunk, ""), text)
        elif c.field_match and extraction is not None:
            snippet = field_snippet(
                FIELD_LABELS[c.field_match], str(getattr(extraction, c.field_match) or ""), text
            )
        elif c.semantic_chunk:
            snippet = semantic_snippet(contents.get(c.semantic_chunk, ""), text)
        return SearchHit(
            document_id=document.id,
            extraction_id=c.key[1],
            score=c.score,
            match_type=c.match_type,
            unit_index=c.unit_index,
            snippet=snippet,
            filename=document.original_filename,
            invoice_number=extraction.invoice_number if extraction else None,
            invoice_date=extraction.invoice_date if extraction else None,
            supplier_name=extraction.supplier_name if extraction else None,
            customer_name=extraction.customer_name if extraction else None,
            total=extraction.total if extraction else None,
            currency=extraction.currency.strip() if extraction and extraction.currency else None,
            review_status=extraction.review_status if extraction else None,
        )
