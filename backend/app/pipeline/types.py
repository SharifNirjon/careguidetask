"""Plain data returned by the (pure) parsers and consumed by the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.models.enums import ErrorCode, ExtractionMethod, UnitType


class FileKind(StrEnum):
    """Real file type, detected from bytes."""

    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    TIFF = "tiff"
    XLSX = "xlsx"
    XLS = "xls"
    CSV = "csv"

    @property
    def mime_type(self) -> str:
        return _MIME[self]

    @classmethod
    def from_mime(cls, mime_type: str) -> FileKind:
        return next(kind for kind, mime in _MIME.items() if mime == mime_type)

    @property
    def is_image(self) -> bool:
        return self in (FileKind.JPEG, FileKind.PNG, FileKind.TIFF)

    @property
    def is_tabular(self) -> bool:
        return self in (FileKind.XLSX, FileKind.XLS, FileKind.CSV)


_MIME: dict[FileKind, str] = {
    FileKind.PDF: "application/pdf",
    FileKind.JPEG: "image/jpeg",
    FileKind.PNG: "image/png",
    FileKind.TIFF: "image/tiff",
    FileKind.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    FileKind.XLS: "application/vnd.ms-excel",
    FileKind.CSV: "text/csv",
}


def unit_warning(code: str, message: str, **details: Any) -> dict[str, Any]:
    """A warning entry for document_units.warnings (JSONB list)."""
    entry: dict[str, Any] = {"code": code, "message": message}
    if details:
        entry["details"] = details
    return entry


@dataclass(slots=True)
class OcrLine:
    text: str
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text, "bbox": [round(v, 2) for v in self.bbox]}
        if self.confidence is not None:
            out["confidence"] = round(self.confidence, 3)
        return out


@dataclass(slots=True)
class ParsedUnit:
    """One document_units row, before it is persisted."""

    unit_type: UnitType
    unit_index: int
    extraction_method: ExtractionMethod
    text: str | None = None
    sheet_name: str | None = None
    ocr_confidence: float | None = None
    structured_data: dict[str, Any] | None = None
    image_quality: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    error_code: ErrorCode | None = None
    duration_ms: int = 0

    @property
    def is_failed(self) -> bool:
        return self.error_code is not None

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())
