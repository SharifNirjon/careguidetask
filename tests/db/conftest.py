"""Fixtures for database tests. These run against REAL PostgreSQL + pgvector.

    docker compose up -d db
    pytest tests/db

TEST_DATABASE_URL (default below) names a throwaway database: it is dropped and
recreated from the Alembic migrations at the start of the session. Each test
runs inside a transaction that is rolled back afterwards, so tests are isolated
and leave no data behind.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import normalize_db_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
DEFAULT_URL = "postgresql+psycopg://careguide:careguide@localhost:5432/careguide_test"


def _test_url() -> URL:
    return make_url(normalize_db_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)))


def _recreate_database(url: URL) -> None:
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError as exc:
        pytest.exit(
            f"Cannot reach PostgreSQL at {url.render_as_string(hide_password=True)}.\n"
            "Start it with `docker compose up -d db` or set TEST_DATABASE_URL.\n"
            f"{exc}",
            returncode=1,
        )
    finally:
        admin.dispose()


def _drop_database(url: URL) -> None:
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
    admin.dispose()


def alembic_config(connection: Any) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg


def make_migrated_engine(url: URL) -> Engine:
    """Fresh database at `url`, migrated to head with Alembic (not create_all)."""
    _recreate_database(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    return engine


@pytest.fixture(scope="session")
def test_url() -> URL:
    return _test_url()


@pytest.fixture(scope="session")
def engine(test_url: URL) -> Iterator[Engine]:
    eng = make_migrated_engine(test_url)
    yield eng
    eng.dispose()
    _drop_database(test_url)


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """Session inside an outer transaction that is always rolled back.

    join_transaction_mode="create_savepoint" means session.commit()/rollback()
    in the code under test only affect a SAVEPOINT, so the test can provoke
    IntegrityErrors, call session.rollback(), and keep going.
    """
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def committed_engine(engine: Engine) -> Iterator[Engine]:
    """For tests that need several real transactions (locking, concurrency).

    Data is really committed, then wiped afterwards.
    """
    yield engine
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE documents CASCADE"))
