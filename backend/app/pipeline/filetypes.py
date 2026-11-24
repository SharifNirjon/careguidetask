"""Detect the real file type from its bytes. The extension is only cross-checked.

Rules:
- content that matches nothing we support -> UNSUPPORTED_TYPE, unless the
  extension claims a supported type (e.g. an .exe renamed .pdf) -> TYPE_MISMATCH
- supported content + known extension of a different type -> TYPE_MISMATCH
- missing/unknown extension -> trust the content
- macro-enabled / binary Office formats are rejected outright
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import olefile

from app.config import PipelineSettings
from app.models.enums import ErrorCode
from app.pipeline.csv_parser import SNIFF_SAMPLE_BYTES, looks_like_csv
from app.pipeline.errors import PermanentError
from app.pipeline.types import FileKind

PDF_SEARCH_BYTES: Final = 1024  # the spec allows junk before %PDF-
MAX_ZIP_ENTRIES: Final = 10_000

EXTENSIONS: Final[dict[str, FileKind]] = {
    "pdf": FileKind.PDF,
    "jpg": FileKind.JPEG,
    "jpeg": FileKind.JPEG,
    "png": FileKind.PNG,
    "tif": FileKind.TIFF,
    "tiff": FileKind.TIFF,
    "xlsx": FileKind.XLSX,
    "xls": FileKind.XLS,
    "csv": FileKind.CSV,
    "txt": FileKind.CSV,
}
# Formats we refuse even when their content is well-formed (macros / binary Office).
REJECTED_EXTENSIONS: Final = frozenset({"xlsm", "xltm", "xlsb", "xlam", "xla", "xlm"})

_OLE_MAGIC: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC: Final = b"PK\x03\x04"
_MACRO_CONTENT_TYPE: Final = b"macroEnabled"


@dataclass(frozen=True, slots=True)
class Detection:
    kind: FileKind
    details: dict[str, Any] = field(default_factory=dict)


def normalize_extension(filename: str) -> str:
    """Lower-case extension of the user's filename ('' if none/unsafe)."""
    suffix = Path(filename).suffix.lower().lstrip(".")
    return suffix if suffix.isalnum() and len(suffix) <= 10 else ""


def detect(path: Path, extension: str, settings: PipelineSettings) -> Detection:
    """Return the detected type or raise PermanentError."""
    if extension in REJECTED_EXTENSIONS:
        raise PermanentError(
            ErrorCode.UNSUPPORTED_TYPE, details={"reason": "macro_or_binary_office", "ext": extension}
        )
    with open(path, "rb") as fh:
        head = fh.read(SNIFF_SAMPLE_BYTES)

    detection = _sniff(path, head, settings)
    claimed = EXTENSIONS.get(extension)
    if detection is None:
        code = ErrorCode.TYPE_MISMATCH if claimed else ErrorCode.UNSUPPORTED_TYPE
        raise PermanentError(code, details={"ext": extension, "magic": head[:8].hex()})
    if claimed is not None and claimed is not detection.kind:
        raise PermanentError(
            ErrorCode.TYPE_MISMATCH,
            details={"ext": extension, "detected": detection.kind.value},
        )
    return detection


def _sniff(path: Path, head: bytes, settings: PipelineSettings) -> Detection | None:
    if b"%PDF-" in head[:PDF_SEARCH_BYTES]:
        return Detection(FileKind.PDF)
    if head.startswith(b"\xff\xd8\xff"):
        return Detection(FileKind.JPEG)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Detection(FileKind.PNG)
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return Detection(FileKind.TIFF)
    if head.startswith(_ZIP_MAGIC):
        return _sniff_zip(path, settings)
    if head.startswith(_OLE_MAGIC):
        return _sniff_ole(path)
    if looks_like_csv(head):
        return Detection(FileKind.CSV)
    return None


def _sniff_zip(path: Path, settings: PipelineSettings) -> Detection | None:
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            names = {i.filename for i in infos}
            if "xl/workbook.xml" not in names:
                if "xl/workbook.bin" in names:
                    raise PermanentError(ErrorCode.UNSUPPORTED_TYPE, details={"reason": "xlsb"})
                return None
            _check_zip_bomb(infos, settings)
            if any(i.flag_bits & 0x1 for i in infos):
                raise PermanentError(ErrorCode.PASSWORD_PROTECTED, details={"reason": "zip_encrypted"})
            content_types = zf.read("[Content_Types].xml") if "[Content_Types].xml" in names else b""
    except zipfile.BadZipFile as exc:
        raise PermanentError(
            ErrorCode.CORRUPT_FILE, details={"reason": "bad_zip", "error": str(exc)}
        ) from exc
    if "xl/vbaProject.bin" in names or _MACRO_CONTENT_TYPE in content_types:
        raise PermanentError(ErrorCode.UNSUPPORTED_TYPE, details={"reason": "macro_enabled"})
    return Detection(FileKind.XLSX, {"zip_entries": len(names)})


def _check_zip_bomb(infos: list[zipfile.ZipInfo], settings: PipelineSettings) -> None:
    """Reject archives whose declared expansion is unsafe.

    Declared sizes are trustworthy enough here: CPython's zipfile stops reading
    a member at its declared size and fails the CRC check if it was forged.
    """
    if len(infos) > MAX_ZIP_ENTRIES:
        raise PermanentError(ErrorCode.DECOMPRESSION_BOMB, details={"entries": len(infos)})
    total = sum(i.file_size for i in infos)
    limit = settings.max_zip_uncompressed_mb * 1024 * 1024
    if total > limit:
        raise PermanentError(
            ErrorCode.DECOMPRESSION_BOMB, details={"uncompressed_bytes": total, "limit": limit}
        )
    min_member = settings.zip_ratio_min_member_mb * 1024 * 1024
    for info in infos:
        if info.file_size >= min_member and info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > settings.max_zip_ratio:
                raise PermanentError(
                    ErrorCode.DECOMPRESSION_BOMB,
                    details={"member": info.filename, "ratio": round(ratio, 1)},
                )


def _sniff_ole(path: Path) -> Detection | None:
    try:
        with olefile.OleFileIO(path) as ole:
            streams = {"/".join(s) for s in ole.listdir()}
    except (OSError, ValueError) as exc:  # olefile raises OSError subclasses on damage
        raise PermanentError(
            ErrorCode.CORRUPT_FILE, details={"reason": "bad_ole", "error": str(exc)}
        ) from exc
    if "EncryptedPackage" in streams:  # password-protected OOXML (xlsx) lives in OLE2
        raise PermanentError(ErrorCode.PASSWORD_PROTECTED, details={"reason": "encrypted_ooxml"})
    if streams & {"Workbook", "Book"}:
        if "_VBA_PROJECT_CUR" in {s.split("/")[0] for s in streams}:
            raise PermanentError(ErrorCode.UNSUPPORTED_TYPE, details={"reason": "macro_enabled"})
        return Detection(FileKind.XLS)
    return None
