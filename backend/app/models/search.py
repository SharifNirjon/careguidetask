"""chunks: the keyword + semantic search index."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.config import EMBEDDING_DIM
from app.models.base import Base, created_at_col, uuid_pk


class Chunk(Base):
    """A searchable span of text.

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
    unit_id: Mapped[uuid.UUID]
    extraction_id: Mapped[uuid.UUID | None]
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str]
    token_count: Mapped[int | None] = mapped_column(Integer)
    # NULL until the embed stage runs.
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None]
    # Which vector space the embedding lives in (see app.config.EmbeddingModelConfig).
    embedding_space: Mapped[str | None]
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple'::regconfig, content)", persisted=True)
    )
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
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_unit_id", "unit_id"),
        Index("ix_chunks_extraction_id", "extraction_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonneg"),
        CheckConstraint("token_count >= 0", name="token_count_nonneg"),
        CheckConstraint("(embedding IS NULL) = (embedding_model IS NULL)", name="embedding_has_model"),
        CheckConstraint("(embedding IS NULL) = (embedding_space IS NULL)", name="embedding_has_space"),
    )
