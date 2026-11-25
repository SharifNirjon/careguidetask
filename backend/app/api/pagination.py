"""Opaque keyset cursors: base64(JSON) of the last row's sort key."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from app.api.errors import FieldError, ValidationFailed


@dataclass(frozen=True, slots=True)
class Cursor:
    """The sort key of the last item on the previous page (e.g. [created_at, id])."""

    values: tuple[Any, ...]


def encode_cursor(values: list[Any]) -> str:
    raw = json.dumps(values, default=str, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str | None, *, arity: int) -> Cursor | None:
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise invalid_cursor() from exc
    if not isinstance(values, list) or len(values) != arity:
        raise invalid_cursor()
    return Cursor(tuple(values))


def invalid_cursor() -> ValidationFailed:
    return ValidationFailed(
        "The cursor is invalid or expired.",
        code="invalid_cursor",
        errors=[FieldError(("query", "cursor"), "invalid cursor")],
    )
