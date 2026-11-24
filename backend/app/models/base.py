"""Declarative base, naming convention and shared column helpers."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    FetchedValue,
    MetaData,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# Deterministic constraint/index names, so Alembic diffs and downgrades are stable.
NAMING_CONVENTION: dict[str, str] = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}

# Exact-decimal types. NEVER float for money.
Money = Numeric(18, 4, asdecimal=True)
Rate = Numeric(7, 4, asdecimal=True)
Confidence = Numeric(4, 3, asdecimal=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        Decimal: Money,
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        str: Text,
    }


E = TypeVar("E", bound=StrEnum)


class StrEnumText(TypeDecorator[E]):
    """Stores a StrEnum as TEXT (the DB enforces values with a CHECK constraint).

    Native PG ENUMs are avoided because adding/removing values needs ALTER TYPE,
    which is awkward in transactional migrations; a CHECK is dropped/re-added.
    """

    impl = Text
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        super().__init__()
        self.enum_cls = enum_cls

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return self.enum_cls(value).value  # raises ValueError on unknown values

    def process_result_value(self, value: Any, dialect: Any) -> E | None:
        return None if value is None else self.enum_cls(value)


def enum_check(column: str, enum_cls: type[StrEnum]) -> CheckConstraint:
    """CHECK (column IN (...)) named ck_<table>_<column>."""
    values = ", ".join(f"'{m.value}'" for m in enum_cls)
    return CheckConstraint(f"{column} IN ({values})", name=column)


def confidence_check(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} >= 0 AND {column} <= 1", name=f"{column}_range")


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_col() -> Mapped[datetime]:
    # Maintained by the `set_updated_at` trigger (see migration 0001), so it is
    # correct for raw SQL updates too. FetchedValue tells the ORM to re-read it.
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        server_onupdate=FetchedValue(),
    )
