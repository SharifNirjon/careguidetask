"""API keys. Only sha256(key) is stored; the key itself is shown once at creation."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, StrEnumText, created_at_col, enum_check, uuid_pk
from app.models.enums import ApiRole


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str]
    key_prefix: Mapped[str] = mapped_column(unique=True)  # public lookup handle, e.g. "cg_3fa2b1c9"
    key_hash: Mapped[str] = mapped_column(CHAR(64))  # sha256 hex of the full key
    role: Mapped[ApiRole] = mapped_column(StrEnumText(ApiRole))
    created_at: Mapped[datetime] = created_at_col()
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    __table_args__ = (enum_check("role", ApiRole),)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
