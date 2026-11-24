"""Engine and session management (sync SQLAlchemy + psycopg 3).

One sync code path serves both FastAPI and Celery:
- Celery workers are synchronous; an async engine there would need an event
  loop per task for no benefit.
- FastAPI runs plain `def` endpoints in its threadpool, so sync DB calls don't
  block the event loop.
- One engine/session style means one set of fixtures and one way to test.

Celery prefork: call `dispose_engine_after_fork()` from the
`worker_process_init` signal so child processes don't reuse sockets
inherited from the parent.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import DBSettings


def build_engine(settings: DBSettings) -> Engine:
    return create_engine(
        settings.url,
        pool_pre_ping=True,  # survive DB restarts / idle-connection reaping
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_recycle=settings.pool_recycle_s,
        connect_args={
            # Server-side guard against runaway queries (per connection).
            "options": f"-c statement_timeout={settings.statement_timeout_ms}",
            "application_name": "careguide",
        },
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return build_engine(DBSettings.from_env())


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after commit (e.g. when
    # serializing an API response), at the cost of possibly stale attributes.
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request.

    The endpoint decides when to commit; anything uncommitted is rolled back
    when the request ends, and errors always roll back.
    """
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Unit of work for Celery tasks and scripts: commit on success, roll back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine_after_fork() -> None:
    """Drop pooled connections inherited from a parent process (Celery prefork)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose(close=False)
