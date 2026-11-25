"""Search contract: queries, hits, and the protocols the service depends on.

The API depends on SearchService only; retrievers depend on EmbeddingClient
only, so tests swap in a deterministic fake and production uses the
OpenRouter gateway.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from sqlalchemy.orm import Session

from app.llm.embeddings import EmbeddingResult, InputType
from app.models.enums import ReviewStatus
from app.search.filters import DocumentFilters


class SearchMode(StrEnum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class MatchType(StrEnum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    mode: SearchMode
    filters: DocumentFilters
    limit: int
    offset: int = 0


@dataclass(frozen=True, slots=True)
class Segment:
    """A piece of snippet text. Rendered as text, never as HTML."""

    text: str
    highlight: bool = False


@dataclass(frozen=True, slots=True)
class SearchHit:
    document_id: uuid.UUID
    extraction_id: uuid.UUID | None
    score: float
    match_type: MatchType
    unit_index: int | None
    snippet: tuple[Segment, ...]
    filename: str
    invoice_number: str | None = None
    invoice_date: date | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    total: Decimal | None = None
    currency: str | None = None
    review_status: ReviewStatus | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: list[SearchHit]
    total: int  # ranked results available (capped at MAX_SEARCH_RESULTS)
    degraded: bool = False
    degraded_reason: str | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)


class SearchService(Protocol):
    def search(self, session: Session, query: SearchQuery) -> SearchResult: ...


class EmbeddingClient(Protocol):
    """What search needs from app.llm.embeddings.EmbeddingGateway."""

    def embed(
        self, texts: list[str], *, input_type: InputType, spaces: Collection[str] | None = None
    ) -> EmbeddingResult: ...
