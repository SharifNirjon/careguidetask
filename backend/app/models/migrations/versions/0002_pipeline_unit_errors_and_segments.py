"""pipeline: unit errors/warnings, document segments, new error codes

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25

Hand-written after autogenerate (which only saw the three columns and never
sees CHECK changes):
- documents.segments JSONB NULL
- document_units.error_code TEXT NULL (+ CHECK), document_units.warnings JSONB NOT NULL DEFAULT '[]'
- error-code CHECKs on documents and processing_jobs widened with the new
  pipeline codes (TYPE_MISMATCH, EMPTY_FILE, TOO_MANY_PAGES, DECOMPRESSION_BOMB,
  STORAGE_ERROR). The value lists are snapshots, not imports from app code.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CODES = (
    "CORRUPT_FILE", "PASSWORD_PROTECTED", "UNSUPPORTED_TYPE", "FILE_TOO_LARGE",
    "NO_TEXT_FOUND", "OCR_ERROR", "LLM_ERROR", "EMBEDDING_ERROR", "TIMEOUT", "INTERNAL_ERROR",
)
NEW_CODES = (
    "CORRUPT_FILE", "PASSWORD_PROTECTED", "UNSUPPORTED_TYPE", "TYPE_MISMATCH", "EMPTY_FILE",
    "FILE_TOO_LARGE", "TOO_MANY_PAGES", "DECOMPRESSION_BOMB", "NO_TEXT_FOUND", "STORAGE_ERROR",
    "OCR_ERROR", "LLM_ERROR", "EMBEDDING_ERROR", "TIMEOUT", "INTERNAL_ERROR",
)
CODE_TABLES = ("documents", "processing_jobs")


def _in_list(codes: Sequence[str]) -> str:
    return "error_code IN (" + ", ".join(f"'{c}'" for c in codes) + ")"


def _replace_code_check(table: str, codes: Sequence[str], *, validate: bool) -> None:
    name = op.f(f"ck_{table}_error_code")  # op.f: already final, skip the naming convention
    op.drop_constraint(name, table, type_="check")
    if validate:
        op.create_check_constraint(name, table, _in_list(codes))
    else:
        # Downgrade: rows may already hold new codes; enforce for new rows only.
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({_in_list(codes)}) NOT VALID")


def upgrade() -> None:
    op.add_column("documents", sa.Column("segments", postgresql.JSONB(), nullable=True))
    op.add_column("document_units", sa.Column("error_code", sa.Text(), nullable=True))
    op.add_column(
        "document_units",
        sa.Column("warnings", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"),
                  nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_document_units_error_code"), "document_units", _in_list(NEW_CODES)
    )
    for table in CODE_TABLES:
        _replace_code_check(table, NEW_CODES, validate=True)


def downgrade() -> None:
    for table in CODE_TABLES:
        _replace_code_check(table, OLD_CODES, validate=False)
    op.drop_constraint(op.f("ck_document_units_error_code"), "document_units", type_="check")
    op.drop_column("document_units", "warnings")
    op.drop_column("document_units", "error_code")
    op.drop_column("documents", "segments")
