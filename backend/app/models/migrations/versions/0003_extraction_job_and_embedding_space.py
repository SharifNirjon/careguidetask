"""extraction job link, 1024-dim embeddings with a space label, LLM error codes

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-25

- extractions.processing_job_id (FK -> processing_jobs, SET NULL, indexed): makes
  a retried extraction job idempotent.
- chunks.embedding VECTOR(384) -> VECTOR(1024); the HNSW index is dropped and
  recreated. Existing vectors cannot be cast between dimensions, so they are
  cleared (with embedding_model) and must be re-embedded: unavoidable, and
  chunks hold derived data only.
- chunks.embedding_space TEXT, set exactly when an embedding is present
  (CHECK, same rule as embedding_model): chunks exist before the embed stage.
- error-code CHECKs widened with LLM_QUOTA_DEFERRED, LLM_UNAVAILABLE,
  NOT_AN_INVOICE (value lists are snapshots, not imports).
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_DIM = 384
NEW_DIM = 1024
HNSW = "ix_chunks_embedding_hnsw"

CODES_0002 = (
    "CORRUPT_FILE", "PASSWORD_PROTECTED", "UNSUPPORTED_TYPE", "TYPE_MISMATCH", "EMPTY_FILE",
    "FILE_TOO_LARGE", "TOO_MANY_PAGES", "DECOMPRESSION_BOMB", "NO_TEXT_FOUND", "STORAGE_ERROR",
    "OCR_ERROR", "LLM_ERROR", "EMBEDDING_ERROR", "TIMEOUT", "INTERNAL_ERROR",
)
CODES_0003 = (*CODES_0002, "LLM_QUOTA_DEFERRED", "LLM_UNAVAILABLE", "NOT_AN_INVOICE")
CODE_TABLES = ("documents", "processing_jobs", "document_units")


def _in_list(codes: Sequence[str]) -> str:
    return "error_code IN (" + ", ".join(f"'{c}'" for c in codes) + ")"


def _replace_code_check(table: str, codes: Sequence[str], *, validate: bool) -> None:
    name = op.f(f"ck_{table}_error_code")
    op.drop_constraint(name, table, type_="check")
    if validate:
        op.create_check_constraint(name, table, _in_list(codes))
    else:
        # Downgrade: rows may hold the newer codes; enforce for new rows only.
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({_in_list(codes)}) NOT VALID")


def _set_embedding_dim(dim: int) -> None:
    op.drop_index(HNSW, table_name="chunks")
    op.execute("UPDATE chunks SET embedding = NULL, embedding_model = NULL WHERE embedding IS NOT NULL")
    op.alter_column(
        "chunks", "embedding",
        type_=pgvector.sqlalchemy.vector.VECTOR(dim=dim),
        postgresql_using="NULL",
    )
    op.create_index(
        HNSW, "chunks", ["embedding"], unique=False, postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def upgrade() -> None:
    op.add_column("extractions", sa.Column("processing_job_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_extractions_processing_job_id_processing_jobs"), "extractions", "processing_jobs",
        ["processing_job_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(op.f("ix_extractions_processing_job_id"), "extractions", ["processing_job_id"])

    op.add_column("chunks", sa.Column("embedding_space", sa.Text(), nullable=True))
    _set_embedding_dim(NEW_DIM)
    op.create_check_constraint(
        op.f("ck_chunks_embedding_has_space"), "chunks", "(embedding IS NULL) = (embedding_space IS NULL)"
    )

    for table in CODE_TABLES:
        _replace_code_check(table, CODES_0003, validate=True)


def downgrade() -> None:
    for table in CODE_TABLES:
        _replace_code_check(table, CODES_0002, validate=False)

    op.drop_constraint(op.f("ck_chunks_embedding_has_space"), "chunks", type_="check")
    _set_embedding_dim(OLD_DIM)
    op.drop_column("chunks", "embedding_space")

    op.drop_index(op.f("ix_extractions_processing_job_id"), table_name="extractions")
    op.drop_constraint(op.f("fk_extractions_processing_job_id_processing_jobs"), "extractions",
                       type_="foreignkey")
    op.drop_column("extractions", "processing_job_id")
