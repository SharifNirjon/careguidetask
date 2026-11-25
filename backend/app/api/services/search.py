"""Search contract (SearchService) and today's keyword implementation.

Step 7 plugs semantic/hybrid in behind the same protocol and extends keyword
search to full text; the router and response shape don't change.

Keyword: pg_trgm similarity / ILIKE over invoice_number, supplier and
customer, combined with the same filters as GET /documents. Snippets are
built from FIELD VALUES only (never document text) and HTML-escaped before
<mark> is added, because extracted values are untrusted input.
"""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol

from sqlalchemy import ColumnElement, case, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from app.api.errors import NotImplementedYet
from app.api.repositories.documents import document_conditions, escape_like, extraction_conditions
from app.api.services.filters import DocumentFilters
from app.extraction.normalize import invoice_number_norm, supplier_name_norm
from app.models import Document, Extraction
from app.models.enums import ReviewStatus

EXACT_SUBSTRING_SCORE: Final = 1.0
SNIPPET_FIELDS: Final = ("invoice_number", "supplier_name", "customer_name")
FIELD_LABELS: Final = {"invoice_number": "Invoice", "supplier_name": "Supplier", "customer_name": "Customer"}


class SearchMode(StrEnum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"  # Step 7
    HYBRID = "hybrid"  # Step 7


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    mode: SearchMode
    filters: DocumentFilters
    limit: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    document_id: uuid.UUID
    extraction_id: uuid.UUID
    score: float
    snippet: str  # HTML-escaped, with <mark> around the match
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    customer_name: str | None
    total: Decimal | None
    currency: str | None
    review_status: ReviewStatus


class SearchService(Protocol):
    def search(self, session: Session, query: SearchQuery) -> list[SearchHit]: ...


def highlight(value: str, needle: str) -> str:
    """Escape `value`, then wrap the first case-insensitive occurrence of `needle`."""
    match = re.search(re.escape(needle), value, re.IGNORECASE) if needle else None
    if match is None:
        return html.escape(value)
    start, end = match.span()
    return (
        f"{html.escape(value[:start])}<mark>{html.escape(value[start:end])}</mark>{html.escape(value[end:])}"
    )


def build_snippet(values: dict[str, str | None], needle: str) -> str:
    """Prefer a field that literally contains the query; else the first non-empty one."""
    present = [(name, v) for name in SNIPPET_FIELDS if (v := values.get(name))]
    if not present:
        return ""
    name, value = next(((n, v) for n, v in present if needle.casefold() in v.casefold()), present[0])
    return f"{FIELD_LABELS[name]}: {highlight(value, needle)}"


class KeywordSearchService:
    def __init__(self, *, min_similarity: float) -> None:
        self._min_similarity = min_similarity

    def search(self, session: Session, query: SearchQuery) -> list[SearchHit]:
        if query.mode is not SearchMode.KEYWORD:
            raise NotImplementedYet(
                f"Search mode {query.mode.value!r} arrives in Step 7.", code="search_mode_not_implemented"
            )
        text = query.text.strip()
        e = aliased(Extraction)
        score = self._score(e, text)
        like = f"%{escape_like(text)}%"
        matches = or_(
            e.invoice_number.ilike(like, escape="\\"),
            e.supplier_name.ilike(like, escape="\\"),
            e.customer_name.ilike(like, escape="\\"),
            score >= self._min_similarity,
        )
        relevance = func.greatest(score, self._exact_bonus(e, like)).label("score")
        rows = session.execute(
            select(e, relevance)
            .join(Document, Document.id == e.document_id)
            .where(
                e.is_current.is_(True),
                matches,
                *document_conditions(query.filters),
                *extraction_conditions(e, query.filters),
            )
            .order_by(relevance.desc(), e.id)
            .limit(query.limit)
        ).all()
        return [self._hit(extraction, float(value), text) for extraction, value in rows]

    @staticmethod
    def _score(e: Any, text: str) -> ColumnElement[float]:
        number_key = invoice_number_norm(text) or ""
        supplier_key = supplier_name_norm(text) or text.casefold()
        return func.greatest(
            func.similarity(func.coalesce(e.invoice_number_norm, ""), number_key),
            func.similarity(func.coalesce(e.supplier_name_norm, ""), supplier_key),
            func.similarity(func.lower(func.coalesce(e.customer_name, "")), text.casefold()),
        )

    @staticmethod
    def _exact_bonus(e: Any, like: str) -> ColumnElement[float]:
        """A literal substring hit outranks any fuzzy one."""
        hit = or_(
            e.invoice_number.ilike(like, escape="\\"),
            e.supplier_name.ilike(like, escape="\\"),
            e.customer_name.ilike(like, escape="\\"),
        )
        return case((hit, literal(EXACT_SUBSTRING_SCORE)), else_=literal(0.0))

    @staticmethod
    def _hit(extraction: Extraction, score: float, text: str) -> SearchHit:
        values = {name: getattr(extraction, name) for name in SNIPPET_FIELDS}
        return SearchHit(
            document_id=extraction.document_id,
            extraction_id=extraction.id,
            score=round(min(score, EXACT_SUBSTRING_SCORE), 4),
            snippet=build_snippet(values, text),
            invoice_number=extraction.invoice_number,
            invoice_date=extraction.invoice_date,
            supplier_name=extraction.supplier_name,
            customer_name=extraction.customer_name,
            total=extraction.total,
            currency=extraction.currency.strip() if extraction.currency else None,
            review_status=extraction.review_status,
        )
