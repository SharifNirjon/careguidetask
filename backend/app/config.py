"""Application configuration.

Runtime settings come from environment variables. EMBEDDING_DIM is deliberately
NOT read from the environment: it is baked into the `chunks.embedding` column
type, so changing it requires a schema migration (see docs/database.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

# Dimension of the sentence-embedding model (all-MiniLM-L6-v2 -> 384).
# Changing this value requires: a new Alembic migration that alters
# chunks.embedding to VECTOR(<new dim>) (dropping/recreating the HNSW index),
# and a full re-embed of every chunk. tests/db/test_migrations.py fails if the
# database and this constant disagree.
EMBEDDING_DIM: Final[int] = 384
EMBEDDING_MODEL_DEFAULT: Final[str] = "sentence-transformers/all-MiniLM-L6-v2"


def normalize_db_url(url: str) -> str:
    """Force the psycopg 3 driver; accept plain postgres:// / postgresql:// URLs."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True, slots=True)
class DBSettings:
    url: str
    pool_size: int
    max_overflow: int
    pool_recycle_s: int
    statement_timeout_ms: int

    @classmethod
    def from_env(cls) -> DBSettings:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not set (see .env.example)")
        return cls(
            url=normalize_db_url(url),
            pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
            max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
            pool_recycle_s=int(os.environ.get("DB_POOL_RECYCLE_S", "1800")),
            statement_timeout_ms=int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000")),
        )
