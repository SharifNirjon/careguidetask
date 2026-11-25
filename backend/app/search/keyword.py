"""Keyword retrieval: full text over chunks + field matches on extractions.

Full text: websearch_to_tsquery('simple', q), so users get "quoted phrases",
OR and -exclusion; ranked with ts_rank_cd (cover density: terms close together
rank higher). The 'simple' parser reads "INV-2026-001" as 'inv' '-2026' '-001'
(signed numbers), so chunks.tsv treats '-' and '/' between letters/digits as
spaces, and prepare_websearch() rewrites such codes in the query into a quoted
phrase: "INV-2026-001" -> "INV 2026 001" (all parts, in order), while
"2026-001" and "001" still find it. A leading '-' stays an exclusion.

Fields: the exact normalized invoice number (boosted to the top by the service
when the query looks like one), and trigram similarity on the supplier key and
lower(customer_name) - both served by GIN trigram indexes.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import ColumnElement, case, exists, func, literal, literal_column, or_, select, text
from sqlalchemy.orm import Session, aliased

from app.extraction.normalize import invoice_number_norm, supplier_name_norm
from app.models import Chunk, Document, DocumentUnit, Extraction
from app.search.filters import DocumentFilters, document_conditions, escape_like, extraction_conditions

CODE_SEPARATOR: Final = re.compile(r"([^\W_])[-/](?=[^\W_])")
_QUERY_TOKEN: Final = re.compile(r'-?"[^"]*"?|\S+')
INVOICE_NUMBER_SHAPE: Final = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-/._ ]{1,38}[A-Za-z0-9]$")
MIN_INVOICE_NUMBER_CHARS: Final = 3
RANK_NORMALIZATION: Final = 32  # ts_rank_cd(..., 32): rank / (rank + 1), so scores land in [0, 1)
EXACT_NUMBER_SCORE: Final = 1.0
SIMPLE: Final[ColumnElement[Any]] = literal_column("'simple'::regconfig")


def split_codes(text: str) -> str:
    """Same rule as the chunks.tsv expression: 'INV-2026-001' -> 'INV 2026 001'."""
    return CODE_SEPARATOR.sub(r"\1 ", text)


def prepare_websearch(query: str) -> str:
    """Rewrite code-like words into phrases; keep quotes, OR and -exclusions."""
    out = []
    for token in _QUERY_TOKEN.findall(query):
        sign, core = ("-", token[1:]) if token.startswith("-") and len(token) > 1 else ("", token)
        if core.startswith('"'):
            closed = core if core.endswith('"') and len(core) > 1 else core + '"'
            out.append(sign + split_codes(closed))
            continue
        split = split_codes(core)
        out.append(f'{sign}"{split}"' if split != core else token)
    return " ".join(out)


def exclusions(prepared: str) -> list[str]:
    """The -terms of a prepared query, without their sign ('-"INV 9"' -> '"INV 9"')."""
    return [t[1:] for t in _QUERY_TOKEN.findall(prepared) if t.startswith("-") and len(t) > 1]


def looks_like_invoice_number(query: str) -> bool:
    """One code-like term with at least one digit ('INV-2026-001', '20260001')."""
    stripped = query.strip().strip('"')
    if " " in stripped.strip() and not CODE_SEPARATOR.search(stripped):
        return False
    norm = invoice_number_norm(stripped) or ""
    return len(norm) >= MIN_INVOICE_NUMBER_CHARS and bool(INVOICE_NUMBER_SHAPE.match(stripped))


@dataclass(frozen=True, slots=True)
class ChunkHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    extraction_id: uuid.UUID | None
    unit_index: int | None
    score: float  # list-local score, used only to order the list (RRF uses ranks)


@dataclass(frozen=True, slots=True)
class FieldHit:
    document_id: uuid.UUID
    extraction_id: uuid.UUID
    unit_index: int
    score: float
    exact_number: bool
    matched_field: str


def chunk_filters(filters: DocumentFilters) -> list[ColumnElement[bool]]:
    """Document filters + invoice-field filters on the chunk's own extraction."""
    conditions = document_conditions(filters)
    if filters.touches_extractions:
        e = aliased(Extraction)
        conditions.append(
            exists(
                select(e.id).where(
                    e.id == Chunk.extraction_id, e.is_current.is_(True), *extraction_conditions(e, filters)
                )
            )
        )
    return conditions


def chunk_base(score: ColumnElement[Any], filters: DocumentFilters) -> Any:
    return (
        select(
            Chunk.id, Chunk.document_id, Chunk.extraction_id, DocumentUnit.unit_index, score.label("score")
        )
        .join(Document, Document.id == Chunk.document_id)
        .join(DocumentUnit, DocumentUnit.id == Chunk.unit_id)
        .where(*chunk_filters(filters))
    )


class KeywordRetriever:
    def __init__(self, *, min_similarity: float) -> None:
        self._min_similarity = min_similarity

    def fulltext(self, session: Session, query: str, filters: DocumentFilters, limit: int) -> list[ChunkHit]:
        prepared = prepare_websearch(query)
        tsquery = func.websearch_to_tsquery(SIMPLE, prepared)
        rank = func.ts_rank_cd(Chunk.tsv, tsquery, RANK_NORMALIZATION)
        statement = chunk_base(rank, filters).where(Chunk.tsv.op("@@")(tsquery))
        if excluded := exclusions(prepared):
            # "-word" excludes DOCUMENTS: without this, a document's other chunks (its
            # summary, say) would still match and bring back what the user excluded.
            other = aliased(Chunk)
            banned = func.websearch_to_tsquery(SIMPLE, " or ".join(excluded))
            statement = statement.where(
                ~exists(
                    select(other.id).where(other.document_id == Chunk.document_id, other.tsv.op("@@")(banned))
                )
            )
        rows = session.execute(statement.order_by(rank.desc(), Chunk.id).limit(limit)).all()
        return [ChunkHit(r[0], r[1], r[2], r[3], float(r[4])) for r in rows]

    def fields(self, session: Session, query: str, filters: DocumentFilters, limit: int) -> list[FieldHit]:
        session.execute(
            text("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"),
            {"t": str(self._min_similarity)},
        )
        e = aliased(Extraction)
        number = invoice_number_norm(query) or ""
        supplier = supplier_name_norm(query) or query.casefold()
        customer = query.casefold()
        customer_col = func.lower(e.customer_name)
        exact = e.invoice_number_norm == number if number else literal(False)
        supplier_score = func.coalesce(func.similarity(e.supplier_name_norm, supplier), 0.0)
        customer_score = func.coalesce(func.similarity(customer_col, customer), 0.0)
        score = func.greatest(case((exact, EXACT_NUMBER_SCORE), else_=0.0), supplier_score, customer_score)
        like = f"%{escape_like(customer)}%"
        rows = session.execute(
            select(
                e.document_id,
                e.id,
                e.unit_start,
                score.label("score"),
                exact.label("exact"),
                supplier_score.label("s"),
                customer_score.label("c"),
            )
            .join(Document, Document.id == e.document_id)
            .where(
                e.is_current.is_(True),
                or_(
                    exact,
                    e.supplier_name_norm.op("%")(supplier),
                    customer_col.op("%")(customer),
                    customer_col.like(like, escape="\\"),
                ),
                *document_conditions(filters),
                *extraction_conditions(e, filters),
            )
            .order_by(score.desc(), e.id)
            .limit(limit)
        ).all()
        return [
            FieldHit(
                r[0],
                r[1],
                r[2],
                float(r[3]),
                bool(r[4]),
                "invoice_number" if r[4] else ("supplier_name" if r[5] >= r[6] else "customer_name"),
            )
            for r in rows
        ]
