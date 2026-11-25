"""chunks: the keyword + semantic search index."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.config import EMBEDDING_DIM, EMBEDDING_SPACES
from app.models.base import Base, StrEnumText, created_at_col, enum_check, uuid_pk
from app.models.enums import ChunkKind

HNSW_PARAMS = {"m": 16, "ef_construction": 64}
# '-' and '/' between letters/digits become spaces before tokenizing: the 'simple'
# parser would turn "INV-2026-001" into 'inv' '-2026' '-001' (signed numbers), so
# neither "2026-001" nor "001" could match. app.search.keyword applies the same
# rule to queries (tests/search/test_keyword.py pins both sides together).
TSV_EXPRESSION = (
    "to_tsvector('simple'::regconfig, "
    "regexp_replace(content, '([[:alnum:]])[-/](?=[[:alnum:]])', '\\1 ', 'g'))"
)


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha_default(context: Any) -> str:
    return content_sha256(str(context.get_current_parameters()["content"]))


def _embedded_at_default(context: Any) -> datetime | None:
    """Rows inserted WITH a vector (re-chunking that reuses one, seeds) get a time."""
    return datetime.now(UTC) if context.get_current_parameters().get("embedding") is not None else None


def hnsw_index_name(space: str) -> str:
    """ix_chunks_embedding_hnsw_<space>, with '.' and '-' as '_'."""
    return "ix_chunks_embedding_hnsw_" + space.replace(".", "_").replace("-", "_")


def space_indexes() -> list[Index]:
    """One PARTIAL HNSW index per space: a vector query always filters on one
    space, and a per-space index keeps the graph free of incomparable vectors."""
    return [
        Index(
            hnsw_index_name(space),
            "embedding",
            postgresql_using="hnsw",
            postgresql_with=HNSW_PARAMS,
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text(f"embedding_space = '{space}'"),
        )
        for space in EMBEDDING_SPACES
    ]


class Chunk(Base):
    """A searchable span of text (see docs/search.md).

    tsv uses the 'simple' text-search config: no stemming and no stop words.
    Invoices are full of codes (INV-2024-001), names and mixed languages, where
    stemming produces false matches; the cost is that "invoices" won't match
    "invoice" (semantic search covers that gap in hybrid search).

    Only compare embeddings from the same embedding_space: vectors from different
    models live in different spaces even at equal dimension. Filter on it.
    """

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    # Summary chunks point at the first unit of their extraction (the page a preview shows).
    unit_id: Mapped[uuid.UUID]
    # Set when the unit lies inside exactly one current extraction (always for summaries).
    extraction_id: Mapped[uuid.UUID | None]
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_kind: Mapped[ChunkKind] = mapped_column(StrEnumText(ChunkKind), default=ChunkKind.CONTENT)
    content: Mapped[str]
    # sha256 of `content`: an unchanged chunk keeps its embedding across re-chunking.
    content_sha256: Mapped[str] = mapped_column(CHAR(64), default=_sha_default)
    token_count: Mapped[int | None] = mapped_column(Integer)
    # NULL until embedded (chunks are keyword-searchable before that).
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None]
    # Which vector space the embedding lives in (see app.config.EMBEDDING_SPACES).
    embedding_space: Mapped[str | None]
    embedded_at: Mapped[datetime | None] = mapped_column(default=_embedded_at_default)
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(TSV_EXPRESSION, persisted=True))
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        # Composite FKs keep the denormalized document_id consistent with the
        # unit / extraction it points at (MATCH SIMPLE: skipped when extraction_id is NULL).
        ForeignKeyConstraint(
            ["unit_id", "document_id"],
            ["document_units.id", "document_units.document_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["extraction_id", "document_id"],
            ["extractions.id", "extractions.document_id"],
            ondelete="CASCADE",
        ),
        # (document_id, chunk_index): ordered per-document reads; leading column is the FK index.
        Index("ix_chunks_document_id_chunk_index", "document_id", "chunk_index"),
        Index("ix_chunks_unit_id", "unit_id"),
        Index("ix_chunks_extraction_id", "extraction_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        # The embedding work queue: small, and only the rows still to do.
        Index("ix_chunks_pending_embedding", "document_id", postgresql_where=text("embedding IS NULL")),
        *space_indexes(),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonneg"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_hex"),
        CheckConstraint("(embedding IS NULL) = (embedded_at IS NULL)", name="embedding_has_time"),
        enum_check("chunk_kind", ChunkKind),
        CheckConstraint("token_count >= 0", name="token_count_nonneg"),
        CheckConstraint("(embedding IS NULL) = (embedding_model IS NULL)", name="embedding_has_model"),
        CheckConstraint("(embedding IS NULL) = (embedding_space IS NULL)", name="embedding_has_space"),
    )
