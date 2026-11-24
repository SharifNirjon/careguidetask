"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

REVIEW CHECKLIST (autogenerate gets these wrong or misses them):
- generated columns (Computed), partial indexes (postgresql_where)
- HNSW / GIN indexes and their operator classes (vector_cosine_ops, gin_trgm_ops)
- triggers and functions (autogenerate never sees them)
- big tables: CREATE INDEX CONCURRENTLY (in an autocommit_block), CHECK ... NOT VALID + VALIDATE
- downgrade() must fully reverse upgrade()
"""
from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
