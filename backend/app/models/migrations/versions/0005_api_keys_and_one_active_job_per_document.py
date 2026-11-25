"""api keys, one active job per document, WORKER_TIMEOUT

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25

- api_keys (+ role CHECK, per the TEXT+CHECK enum convention).
- UNIQUE partial index processing_jobs(document_id) WHERE status IN
  ('queued', 'running'): concurrent reprocess requests can't both succeed.
  Pre-existing duplicates (impossible to create through the API, possible from
  older scripts) would block the index, so all but the newest active job per
  document are marked 'cancelled' first.
- error-code CHECKs (documents, processing_jobs, document_units) widened with
  WORKER_TIMEOUT, which the stale-job sweeper sets.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("viewer", "reviewer", "admin")
CODES_0003 = (
    "CORRUPT_FILE", "PASSWORD_PROTECTED", "UNSUPPORTED_TYPE", "TYPE_MISMATCH", "EMPTY_FILE",
    "FILE_TOO_LARGE", "TOO_MANY_PAGES", "DECOMPRESSION_BOMB", "NO_TEXT_FOUND", "STORAGE_ERROR",
    "OCR_ERROR", "LLM_ERROR", "EMBEDDING_ERROR", "TIMEOUT", "INTERNAL_ERROR",
    "LLM_QUOTA_DEFERRED", "LLM_UNAVAILABLE", "NOT_AN_INVOICE",
)
CODES_0005 = (*CODES_0003, "WORKER_TIMEOUT")
CODE_TABLES = ("documents", "processing_jobs", "document_units")
ACTIVE_JOB_INDEX = "uq_processing_jobs_document_id_active"


def _in_list(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace_code_check(table: str, codes: Sequence[str], *, validate: bool) -> None:
    name = op.f(f"ck_{table}_error_code")
    op.drop_constraint(name, table, type_="check")
    if validate:
        op.create_check_constraint(name, table, _in_list("error_code", codes))
    else:  # downgrade: rows may hold WORKER_TIMEOUT; enforce for new rows only
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({_in_list('error_code', codes)}) NOT VALID")


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in_list("role", ROLES), name=op.f("ck_api_keys_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_prefix", name=op.f("uq_api_keys_key_prefix")),
    )
    op.execute("""
        UPDATE processing_jobs SET status = 'cancelled', finished_at = now()
        WHERE status IN ('queued', 'running') AND id NOT IN (
            SELECT DISTINCT ON (document_id) id FROM processing_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY document_id, queued_at DESC, id DESC
        )
    """)
    op.create_index(ACTIVE_JOB_INDEX, "processing_jobs", ["document_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running')"))
    for table in CODE_TABLES:
        _replace_code_check(table, CODES_0005, validate=True)


def downgrade() -> None:
    for table in CODE_TABLES:
        _replace_code_check(table, CODES_0003, validate=False)
    op.drop_index(ACTIVE_JOB_INDEX, table_name="processing_jobs")
    op.drop_table("api_keys")
