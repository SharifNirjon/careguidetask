"""API key format, hashing and verification (pure).

Key: "cg_<8 hex prefix>_<43-char urlsafe secret>" (~256 bits of entropy).
Only sha256(key) is stored. Lookup is by the public prefix; the hash is then
compared in constant time, so timing reveals nothing about the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from typing import Final

from app.models.enums import ApiRole

KEY_TAG: Final = "cg"
PREFIX_HEX_CHARS: Final = 8
SECRET_BYTES: Final = 32


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    key: str  # shown ONCE to the operator, never stored
    prefix: str
    key_hash: str


@dataclass(frozen=True, slots=True)
class Principal:
    key_id: uuid.UUID
    name: str
    role: ApiRole

    def can(self, required: ApiRole) -> bool:
        return self.role.allows(required)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> GeneratedKey:
    prefix = f"{KEY_TAG}_{secrets.token_hex(PREFIX_HEX_CHARS // 2)}"
    key = f"{prefix}_{secrets.token_urlsafe(SECRET_BYTES)}"
    return GeneratedKey(key=key, prefix=prefix, key_hash=hash_key(key))


def prefix_of(key: str) -> str | None:
    """The lookup prefix, or None when the token doesn't look like one of our keys."""
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_TAG or len(parts[1]) != PREFIX_HEX_CHARS or not parts[2]:
        return None
    return f"{parts[0]}_{parts[1]}"


def verify(key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_key(key), stored_hash)


def bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
