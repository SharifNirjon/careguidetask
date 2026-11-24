"""PDF parsing with PyMuPDF. Pure: path in, ParsedUnit dataclasses out.

Memory: pages are processed one at a time and each rendered pixmap is
released before the next page, so at most one rendered page exists per
process regardless of document size.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np
import pymupdf

from app.config import PipelineSettings
from app.models.enums import ErrorCode, ExtractionMethod, UnitType
from app.pipeline.errors import PermanentError, PipelineError
from app.pipeline.ocr.base import OcrEngine
from app.pipeline.ocr.runner import ocr_page_image
from app.pipeline.text_utils import assess_text, clean_text
from app.pipeline.types import ParsedUnit, unit_warning

log = logging.getLogger(__name__)

POINTS_PER_INCH: Final = 72
REPAIR_MARKER: Final = "repair"
TEXT_FLAGS: Final = (
    pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
)  # deliberately NOT TEXT_PRESERVE_IMAGES: image bytes would bloat memory

pymupdf.TOOLS.mupdf_display_errors(False)  # collect warnings instead of spamming stderr


@dataclass(slots=True)
class PdfInfo:
    page_count: int
    warnings: list[str] = field(default_factory=list)

    @property
    def repaired(self) -> bool:
        return any(REPAIR_MARKER in w for w in self.warnings)


def _drain_mupdf_warnings() -> list[str]:
    text = pymupdf.TOOLS.mupdf_warnings(reset=True)
    return [line for line in text.splitlines() if line.strip()]


def open_pdf(path: Path, settings: PipelineSettings) -> tuple[pymupdf.Document, PdfInfo]:
    """Open and validate a PDF. Raises PermanentError for unusable files."""
    _drain_mupdf_warnings()
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several types for damaged input
        raise PermanentError(ErrorCode.CORRUPT_FILE, details={"error": str(exc)}) from exc
    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise PermanentError(ErrorCode.PASSWORD_PROTECTED)
        info = PdfInfo(page_count=doc.page_count, warnings=_drain_mupdf_warnings())
        if info.page_count == 0:
            raise PermanentError(ErrorCode.CORRUPT_FILE, details={"reason": "no pages"})
        if info.page_count > settings.max_pages:
            raise PermanentError(
                ErrorCode.TOO_MANY_PAGES,
                details={"page_count": info.page_count, "max_pages": settings.max_pages},
            )
    except BaseException:
        doc.close()
        raise
    return doc, info


def inspect_pdf(path: Path, settings: PipelineSettings) -> PdfInfo:
    """Validate the file and count pages without parsing any of them."""
    doc, info = open_pdf(path, settings)
    doc.close()
    return info


# ------------------------------------------------------------ classification


@dataclass(slots=True)
class PageDecision:
    method: ExtractionMethod
    reason: str
    text: str
    lines: list[dict[str, Any]]


def _digital_text(page: pymupdf.Page) -> tuple[str, list[dict[str, Any]]]:
    """Reading-order text plus line-level bboxes (PDF points) from the text layer."""
    content = page.get_text("dict", sort=True, flags=TEXT_FLAGS)
    lines: list[dict[str, Any]] = []
    for block in content.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line.get("spans", []))
            if text.strip():
                lines.append({"text": text, "bbox": [round(v, 2) for v in line["bbox"]]})
    return "\n".join(line["text"] for line in lines), lines


def _image_coverage(page: pymupdf.Page) -> float:
    page_area = abs(page.rect)
    if page_area <= 0:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"]) & page.rect
        covered += abs(rect)
    return min(1.0, covered / page_area)


def classify_page(page: pymupdf.Page, settings: PipelineSettings) -> PageDecision:
    """Digital if the text layer is long enough and not garbage, else OCR."""
    text, lines = _digital_text(page)
    quality = assess_text(text)
    if quality.chars < settings.min_digital_chars:
        return PageDecision(ExtractionMethod.OCR, "too_little_text", text, lines)
    if quality.is_garbage(settings.garbage_min_alnum_ratio, settings.garbage_max_replacement_ratio):
        return PageDecision(ExtractionMethod.OCR, "garbage_text_layer", text, lines)
    if (
        quality.chars < settings.sparse_text_chars
        and _image_coverage(page) > settings.image_coverage_ocr_ratio
    ):
        return PageDecision(ExtractionMethod.OCR, "image_dominated_sparse_text", text, lines)
    return PageDecision(ExtractionMethod.DIGITAL, "text_layer_ok", text, lines)


# ------------------------------------------------------------------ parsing


def _render_gray(
    page: pymupdf.Page, settings: PipelineSettings
) -> tuple[np.ndarray, int, list[dict[str, Any]]]:
    """Render at OCR_DPI (lowered if the page would exceed MAX_IMAGE_PIXELS)."""
    warnings: list[dict[str, Any]] = []
    dpi = settings.ocr_dpi
    w_in, h_in = page.rect.width / POINTS_PER_INCH, page.rect.height / POINTS_PER_INCH
    max_dpi = int((settings.max_image_pixels / max(w_in * h_in, 1e-6)) ** 0.5)
    if max_dpi < dpi:
        warnings.append(
            unit_warning(
                "RENDER_DPI_REDUCED",
                "Page too large; rendered at lower DPI",
                requested_dpi=dpi,
                used_dpi=max_dpi,
            )
        )
        dpi = max(1, max_dpi)
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    try:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)
        gray = arr[:, : pix.width].copy()
    finally:
        del pix  # free the pixmap now; never hold more than one per process
    return gray, dpi, warnings


def _parse_page(page: pymupdf.Page, index: int, engine: OcrEngine, settings: PipelineSettings) -> ParsedUnit:
    decision = classify_page(page, settings)
    warnings: list[dict[str, Any]] = []
    structured: dict[str, Any] | None = None
    image_quality: dict[str, Any] | None = None
    confidence: float | None = None

    if decision.method is ExtractionMethod.DIGITAL:
        raw_text = decision.text
        lines = decision.lines
    else:
        gray, dpi, warnings = _render_gray(page, settings)
        outcome = ocr_page_image(gray, dpi=dpi, engine=engine, settings=settings)
        del gray
        scale = POINTS_PER_INCH / dpi  # OCR pixels -> PDF points
        raw_text = outcome.result.text
        confidence = outcome.result.mean_confidence
        lines = [
            {**line.to_dict(), "bbox": [round(v * scale, 2) for v in line.bbox]}
            for line in outcome.result.lines
        ]
        image_quality = {**outcome.image_quality, "render_dpi": dpi, "classification": decision.reason}

    cleaned = clean_text(raw_text, settings.max_unit_text_chars)
    warnings.extend(cleaned.warnings)
    if settings.store_layout:
        structured = {
            "lines": lines,
            "coord_space": "pdf_points",
            "page_size": [round(page.rect.width, 2), round(page.rect.height, 2)],
            "classification": decision.reason,
        }
    return ParsedUnit(
        unit_type=UnitType.PDF_PAGE,
        unit_index=index,
        extraction_method=decision.method,
        text=cleaned.text,
        ocr_confidence=confidence,
        structured_data=structured,
        image_quality=image_quality,
        warnings=warnings,
    )


def parse_pdf_pages(
    path: Path,
    start: int,
    end: int,
    *,
    engine: OcrEngine,
    settings: PipelineSettings,
) -> list[ParsedUnit]:
    """Parse pages [start, end) of the PDF. A failing page becomes a failed unit."""
    doc, info = open_pdf(path, settings)
    units: list[ParsedUnit] = []
    try:
        for index in range(start, min(end, info.page_count)):
            units.append(_parse_page_safely(doc, index, engine, settings))
    finally:
        doc.close()
    return units


def _parse_page_safely(
    doc: pymupdf.Document, index: int, engine: OcrEngine, settings: PipelineSettings
) -> ParsedUnit:
    started = time.perf_counter()
    try:
        page = doc.load_page(index)
    except Exception as exc:
        unit = _failed_unit(index, ErrorCode.CORRUPT_FILE, str(exc))
    else:
        try:
            unit = _parse_page(page, index, engine, settings)
        except PipelineError as exc:
            unit = _failed_unit(index, exc.code, exc.message, method=ExtractionMethod.OCR)
        except Exception as exc:  # never let one page take the batch down
            log.exception("page parse failed", extra={"unit_index": index, "stage": "parse"})
            unit = _failed_unit(index, ErrorCode.INTERNAL_ERROR, type(exc).__name__)
    unit.warnings.extend(unit_warning("MUPDF_WARNING", w) for w in _drain_mupdf_warnings())
    unit.duration_ms = int((time.perf_counter() - started) * 1000)
    return unit


def _failed_unit(
    index: int,
    code: ErrorCode,
    reason: str,
    *,
    method: ExtractionMethod = ExtractionMethod.DIGITAL,
) -> ParsedUnit:
    return ParsedUnit(
        unit_type=UnitType.PDF_PAGE,
        unit_index=index,
        extraction_method=method,
        error_code=code,
        warnings=[unit_warning("UNIT_FAILED", reason, error_code=code.value)],
    )
