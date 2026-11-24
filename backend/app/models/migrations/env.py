"""Alembic environment.

URL resolution: an explicit connection passed via config.attributes["connection"]
(used by the test suite) wins; otherwise DATABASE_URL from the environment.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import normalize_db_url
from app.models import Base
from app.models.base import StrEnumText

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return normalize_db_url(url)


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    # Migrations must not import app code: render StrEnumText as plain TEXT
    # (the CHECK constraint carries the allowed values).
    if type_ == "type" and isinstance(obj, StrEnumText):
        return "sa.Text()"
    return False


def _configure(**kw: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=False,
        render_item=_render_item,
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
