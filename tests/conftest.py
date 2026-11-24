"""Shared fixtures. Database tests run against REAL PostgreSQL + pgvector.

    docker compose up -d db
    pytest

TEST_DATABASE_URL names a throwaway database: it is dropped and recreated from
the Alembic migrations at the start of the session. The `db` fixture runs each
test inside a transaction that is rolled back afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from tests.support.db import database_url_for_tests, drop_database, make_migrated_engine


@pytest.fixture(scope="session")
def test_url() -> URL:
    return database_url_for_tests()


@pytest.fixture(scope="session")
def engine(test_url: URL) -> Iterator[Engine]:
    eng = make_migrated_engine(test_url)
    yield eng
    eng.dispose()
    drop_database(test_url)


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
