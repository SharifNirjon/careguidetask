"""search: chunk kinds, change detection, per-space HNSW, pending queue

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-25

- chunks.chunk_kind TEXT NOT NULL (content | extraction_summary, CHECK) and
  chunks.content_sha256 CHAR(64): re-chunking keeps the embedding of any chunk
  whose text didn't change. Existing rows are backfilled ('content', sha256).
- chunks.embedded_at TIMESTAMPTZ, set exactly when an embedding is present
  (CHECK, like embedding_model / embedding_space, which stay nullable: chunks
  exist before they are embedded).
- The single HNSW index is replaced by one PARTIAL HNSW index per embedding
  space (WHERE embedding_space = '<space>'). Every vector query filters on one
  space, and vectors of different models must never share a graph.
- ix_chunks_document_id -> ix_chunks_document_id_chunk_index (the composite
  still serves the FK), plus a partial index WHERE embedding IS NULL: the
  embedding work queue.
- extractions: trigram GIN on lower(customer_name) for customer search/filters.
- chunks.tsv is regenerated with '-' and '/' between letters/digits read as
  spaces: the 'simple' parser turns "INV-2026-001" into 'inv' '-2026' '-001'
  (signed numbers), so a search for "2026-001" or "001" could never match.
  Postgres can't ALTER a generated expression in place, so the column and its
  GIN index are dropped and re-created (derived data only).

Value lists and space names are snapshots, never imported from app code.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SPACES = ("nemotron-3.5-8b", "nemotron-3-1b", "nemotron-3.5-1b")
CHUNK_KINDS = ("content", "extraction_summary")
OLD_HNSW = "ix_chunks_embedding_hnsw"
HNSW_WITH = "WITH (m = 16, ef_construction = 64)"
OLD_TSV = "to_tsvector('simple'::regconfig, content)"
NEW_TSV = (
    "to_tsvector('simple'::regconfig, "
    "regexp_replace(content, '([[:alnum:]])[-/](?=[[:alnum:]])', '\\1 ', 'g'))"
)


def _set_tsv(expression: str) -> None:
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_column("chunks", "tsv")
    op.execute(f"ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS ({expression}) STORED")
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")


def _hnsw_name(space: str) -> str:
    return "ix_chunks_embedding_hnsw_" + space.replace(".", "_").replace("-", "_")


def upgrade() -> None:
    op.add_column("chunks", sa.Column("chunk_kind", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("content_sha256", sa.CHAR(64), nullable=True))
    op.add_column("chunks", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE chunks SET chunk_kind = 'content', "
        "content_sha256 = encode(sha256(convert_to(content, 'UTF8')), 'hex'), "
        "embedded_at = CASE WHEN embedding IS NOT NULL THEN created_at END"
    )
    op.alter_column("chunks", "chunk_kind", nullable=False)
    op.alter_column("chunks", "content_sha256", nullable=False)
    kinds = ", ".join(f"'{k}'" for k in CHUNK_KINDS)
    op.create_check_constraint(op.f("ck_chunks_chunk_kind"), "chunks", f"chunk_kind IN ({kinds})")
    op.create_check_constraint(op.f("ck_chunks_content_sha256_hex"), "chunks", "content_sha256 ~ '^[0-9a-f]{64}$'")
    op.create_check_constraint(
        op.f("ck_chunks_embedding_has_time"), "chunks", "(embedding IS NULL) = (embedded_at IS NULL)"
    )

    op.drop_index(OLD_HNSW, table_name="chunks")
    for space in SPACES:
        op.execute(
            f"CREATE INDEX {_hnsw_name(space)} ON chunks USING hnsw (embedding vector_cosine_ops) "
            f"{HNSW_WITH} WHERE embedding_space = '{space}'"
        )
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.create_index("ix_chunks_document_id_chunk_index", "chunks", ["document_id", "chunk_index"])
    op.create_index(
        "ix_chunks_pending_embedding", "chunks", ["document_id"], postgresql_where=sa.text("embedding IS NULL")
    )
    op.execute(
        "CREATE INDEX ix_extractions_customer_name_trgm ON extractions "
        "USING gin (lower(customer_name) gin_trgm_ops)"
    )
    _set_tsv(NEW_TSV)


def downgrade() -> None:
    _set_tsv(OLD_TSV)
    op.drop_index("ix_extractions_customer_name_trgm", table_name="extractions")
    op.drop_index("ix_chunks_pending_embedding", table_name="chunks")
    op.drop_index("ix_chunks_document_id_chunk_index", table_name="chunks")
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    for space in SPACES:
        op.drop_index(_hnsw_name(space), table_name="chunks")
    op.execute(f"CREATE INDEX {OLD_HNSW} ON chunks USING hnsw (embedding vector_cosine_ops) {HNSW_WITH}")
    for name in ("ck_chunks_embedding_has_time", "ck_chunks_content_sha256_hex", "ck_chunks_chunk_kind"):
        op.drop_constraint(op.f(name), "chunks", type_="check")
    op.drop_column("chunks", "embedded_at")
    op.drop_column("chunks", "content_sha256")
    op.drop_column("chunks", "chunk_kind")
