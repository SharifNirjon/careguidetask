"""Database helpers for tests: throwaway databases migrated with Alembic."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

from app.config import normalize_db_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
DEFAULT_URL = "postgresql+psycopg://careguide:careguide@localhost:5432/careguide_test"


def database_url_for_tests() -> URL:
    return make_url(normalize_db_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)))


def recreate_database(url: URL) -> None:
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


def drop_database(url: URL) -> None:
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
    recreate_database(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    return engine
