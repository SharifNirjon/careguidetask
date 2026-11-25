"""Blob storage abstraction.

Keys are generated from UUIDs only. The uploader's filename never becomes part
of a path, so path traversal / reserved names / unicode tricks are impossible.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol

from app.models.enums import ErrorCode
from app.pipeline.errors import TransientError

_KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{32}$")
_COPY_CHUNK = 1024 * 1024
DERIVED_NAMESPACE = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5")


class Storage(Protocol):
    def put(self, stream: BinaryIO, *, key: str | None = None) -> str:
        """Store the stream's bytes; return the key (a new random one unless given)."""
        ...

    def exists(self, key: str) -> bool: ...

    def open(self, key: str) -> BinaryIO:
        """Open a stored blob for binary reading (caller closes)."""
        ...

    def local_path(self, key: str) -> AbstractContextManager[Path]:
        """Context manager yielding a local filesystem path to the blob."""
        ...

    def delete(self, key: str) -> None:
        """Remove a blob; missing keys are ignored."""
        ...


def new_key() -> str:
    """Sharded UUID key, e.g. '3f/a2/3fa2...'. Sharding keeps directories small."""
    hex_ = uuid.uuid4().hex
    return f"{hex_[:2]}/{hex_[2:4]}/{hex_}"


def derived_key(*parts: str) -> str:
    """Deterministic key for derived blobs (e.g. cached page previews): a UUID5 of
    the parts, so it has the same shape as every other key."""
    hex_ = uuid.uuid5(DERIVED_NAMESPACE, "|".join(parts)).hex
    return f"{hex_[:2]}/{hex_[2:4]}/{hex_}"


def validate_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"invalid storage key: {key!r}")
    return key


class LocalStorage:
    """Volume-backed storage. Writes are atomic (temp file + rename)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / validate_key(key)

    def put(self, stream: BinaryIO, *, key: str | None = None) -> str:
        key = key or new_key()
        final = self._path(key)
        tmp = final.with_suffix(".partial")
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as out:
                shutil.copyfileobj(stream, out, _COPY_CHUNK)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, final)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise TransientError(ErrorCode.STORAGE_ERROR, details={"op": "put", "error": str(exc)}) from exc
        except BaseException:
            # e.g. FILE_TOO_LARGE raised by the caller's limiting reader
            tmp.unlink(missing_ok=True)
            raise
        return key

    def open(self, key: str) -> BinaryIO:
        try:
            return open(self._path(key), "rb")
        except OSError as exc:
            raise TransientError(ErrorCode.STORAGE_ERROR, details={"op": "open", "error": str(exc)}) from exc

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        path = self._path(key)
        if not path.is_file():
            raise TransientError(
                ErrorCode.STORAGE_ERROR, details={"op": "local_path", "key": key, "error": "missing"}
            )
        # Already on local disk: no copy needed. A remote backend (S3) would
        # download to a temp file here and delete it on exit.
        yield path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
