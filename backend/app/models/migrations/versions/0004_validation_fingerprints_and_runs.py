"""validation: issue fingerprints, reconciliation runs, new rule codes

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25

- validation_issues.fingerprint TEXT NOT NULL. Existing rows are backfilled in
  SQL with exactly the formula of app.models.issue_fingerprint (a DB test
  proves they agree), so NOT NULL can be added without inventing data.
- UNIQUE partial index (extraction_id, fingerprint) WHERE superseded_at IS NULL:
  one active issue per fingerprint, the backstop when concurrent re-validation
  isn't serialized by the advisory lock (fuzzy matches have no shared key).
- reconciliation_runs (+ status CHECK, per the TEXT+CHECK enum convention) and
  reconciliations.run_id (nullable FK, indexed per the FK-index convention).
- ck_validation_issues_rule_code widened with the Step 5 rule codes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RULE_CODES_0001 = (
    "MISSING_FIELD", "TOTAL_MISMATCH", "LINE_SUM_MISMATCH", "DUPLICATE_INVOICE", "LOW_CONFIDENCE",
    "UNGROUNDED_VALUE", "CROSS_DOC_MISMATCH", "INVALID_DATE", "INVALID_CURRENCY", "INVALID_FORMAT",
)
RULE_CODES_0004 = (
    *RULE_CODES_0001, "NOT_AN_INVOICE", "INVALID_AMOUNT", "NEGATIVE_AMOUNT", "SIGN_INCONSISTENT",
    "IMPLAUSIBLE_TAX_RATE", "ZERO_TOTAL", "LINE_AMOUNT_MISMATCH", "TAX_INCLUSIVE_LINES", "FUTURE_DATE",
    "DATE_TOO_OLD", "UNKNOWN_CURRENCY", "INVOICE_NUMBER_FORMAT", "DUPLICATE_FILE", "POSSIBLE_DUPLICATE",
    "INVOICE_NUMBER_CONFLICT", "CROSS_DOC_VERIFIED",
)
RUN_STATUSES = ("running", "succeeded", "failed")
FINGERPRINT_SQL = (
    "encode(sha256(convert_to(rule_code || '|' || coalesce(field_path, '') || '|' || "
    "coalesce(related_extraction_id::text, ''), 'UTF8')), 'hex')"
)
ACTIVE_FINGERPRINT_INDEX = "uq_validation_issues_extraction_id_fingerprint_active"


def _in_list(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace_rule_code_check(codes: Sequence[str], *, validate: bool) -> None:
    name = op.f("ck_validation_issues_rule_code")
    op.drop_constraint(name, "validation_issues", type_="check")
    if validate:
        op.create_check_constraint(name, "validation_issues", _in_list("rule_code", codes))
    else:  # downgrade: rows may hold the newer codes; enforce for new rows only
        op.execute(
            f"ALTER TABLE validation_issues ADD CONSTRAINT {name} "
            f"CHECK ({_in_list('rule_code', codes)}) NOT VALID"
        )


def upgrade() -> None:
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("left_document_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("right_document_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("summary", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in_list("status", RUN_STATUSES), name=op.f("ck_reconciliation_runs_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_runs")),
    )
    op.add_column("reconciliations", sa.Column("run_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_reconciliations_run_id_reconciliation_runs"), "reconciliations", "reconciliation_runs",
        ["run_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(op.f("ix_reconciliations_run_id"), "reconciliations", ["run_id"])

    op.add_column("validation_issues", sa.Column("fingerprint", sa.Text(), nullable=True))
    op.execute(f"UPDATE validation_issues SET fingerprint = {FINGERPRINT_SQL}")
    op.alter_column("validation_issues", "fingerprint", nullable=False)
    op.create_index(
        ACTIVE_FINGERPRINT_INDEX, "validation_issues", ["extraction_id", "fingerprint"], unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    _replace_rule_code_check(RULE_CODES_0004, validate=True)


def downgrade() -> None:
    _replace_rule_code_check(RULE_CODES_0001, validate=False)
    op.drop_index(ACTIVE_FINGERPRINT_INDEX, table_name="validation_issues")
    op.drop_column("validation_issues", "fingerprint")
    op.drop_index(op.f("ix_reconciliations_run_id"), table_name="reconciliations")
    op.drop_constraint(op.f("fk_reconciliations_run_id_reconciliation_runs"), "reconciliations",
                       type_="foreignkey")
    op.drop_column("reconciliations", "run_id")
    op.drop_table("reconciliation_runs")
