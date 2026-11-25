"""Schema-level guarantees: migrations, model/migration parity, catalog invariants."""

from __future__ import annotations

import re

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL

from app.config import EMBEDDING_DIM
from app.models import Base
from app.models.base import StrEnumText
from tests.support.db import alembic_config, drop_database, make_migrated_engine


def test_upgrade_downgrade_upgrade_round_trip(test_url: URL) -> None:
    scratch = test_url.set(database=f"{test_url.database}_migrations")
    engine = make_migrated_engine(scratch)
    try:
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "base")
        with engine.connect() as conn:
            tables = conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).all()
            functions = conn.scalars(
                text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public'"
                )
            ).all()
            extensions = conn.scalars(text("SELECT extname FROM pg_extension")).all()
        assert tables == ["alembic_version"]
        assert functions == []
        assert "vector" not in extensions and "pg_trgm" not in extensions

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
    finally:
        engine.dispose()
        drop_database(scratch)


def test_models_match_migrations(engine: Engine) -> None:
    """Equivalent of `alembic check`: the ORM models and the migrated DB agree."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_every_foreign_key_is_indexed(engine: Engine) -> None:
    """Each FK's first column leads some non-partial index.

    For composite FKs like chunks(unit_id, document_id) the first column is the
    selective one, so an index on unit_id serves the cascade/lookup.
    """
    sql = text(
        """
        SELECT c.conrelid::regclass::text AS tbl, c.conname
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.connamespace = 'public'::regnamespace
          AND NOT EXISTS (
            SELECT 1
            FROM pg_index i
            WHERE i.indrelid = c.conrelid
              AND i.indpred IS NULL
              AND (string_to_array(i.indkey::text, ' ')::int2[])[1] = c.conkey[1]
          )
        """
    )
    with engine.connect() as conn:
        missing = conn.execute(sql).all()
    assert missing == []


def test_embedding_dimension_matches_config(engine: Engine) -> None:
    with engine.connect() as conn:
        dim = conn.scalar(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
            )
        )
    assert dim == EMBEDDING_DIM, (
        "app.config.EMBEDDING_DIM changed without a migration altering chunks.embedding"
    )


def test_constraint_and_index_names_follow_convention(engine: Engine) -> None:
    prefixes = {"p": "pk_", "f": "fk_", "u": "uq_", "c": "ck_"}
    with engine.connect() as conn:
        constraints = conn.execute(
            text(
                "SELECT conname, contype FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace AND contype IN ('p','f','u','c') "
                "AND conrelid <> 'alembic_version'::regclass"
            )
        ).all()
        indexes = conn.scalars(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).all()
    bad = [n for n, t in constraints if not n.startswith(prefixes[t])]
    bad += [n for n in indexes if not n.startswith(("pk_", "uq_", "ix_"))]
    assert bad == []


def test_enum_checks_match_python_enums(engine: Engine) -> None:
    with engine.connect() as conn:
        defs = dict(
            conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
                )
            ).all()
        )
    checked = 0
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, StrEnumText):
                definition = defs[f"ck_{table.name}_{col.name}"]
                db_values = set(re.findall(r"'([^']+)'", definition))
                assert db_values == {m.value for m in col.type.enum_cls}, (table.name, col.name)
                checked += 1
    assert checked == 16  # update when adding an enum-like column


def test_special_indexes_and_generated_column(engine: Engine) -> None:
    with engine.connect() as conn:
        idx = dict(
            conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")).all()
        )
        tsv_generated = conn.scalar(
            text(
                "SELECT generation_expression FROM information_schema.columns "
                "WHERE table_name = 'chunks' AND column_name = 'tsv' AND is_generated = 'ALWAYS'"
            )
        )
    assert "USING hnsw (embedding vector_cosine_ops)" in idx["ix_chunks_embedding_hnsw"]
    assert "USING gin (tsv)" in idx["ix_chunks_tsv"]
    assert "gin_trgm_ops" in idx["ix_extractions_supplier_name_norm_trgm"]
    assert idx["uq_extractions_document_id_invoice_index_current"].startswith("CREATE UNIQUE")
    assert "WHERE is_current" in idx["uq_extractions_document_id_invoice_index_current"]
    assert "WHERE (superseded_at IS NULL)" in idx["ix_validation_issues_extraction_id_active"]
    assert "created_at DESC" in idx["ix_documents_status_created_at"]
    assert tsv_generated == "to_tsvector('simple'::regconfig, content)"


def test_session_module_applies_statement_timeout(test_url: URL, monkeypatch) -> None:
    from app.config import DBSettings
    from app.db.session import build_engine

    monkeypatch.setenv("DATABASE_URL", test_url.render_as_string(hide_password=False))
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "1234")
    eng = build_engine(DBSettings.from_env())
    try:
        with eng.connect() as conn:
            assert conn.scalar(text("SHOW statement_timeout")) == "1234ms"
    finally:
        eng.dispose()
