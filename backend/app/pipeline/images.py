"""JPEG/PNG/TIFF parsing with Pillow. Pure: path in, ParsedUnit dataclasses out.

Every frame (multi-page TIFF) is one unit (unit_type=image_frame). Frames are
decoded one at a time.
"""

from __future__ import annotations

import logging
import time
import warnings as py_warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import PipelineSettings
from app.models.enums import ErrorCode, ExtractionMethod, UnitType
from app.pipeline.errors import PermanentError, PipelineError
from app.pipeline.ocr.base import OcrEngine
from app.pipeline.ocr.runner import ocr_page_image
from app.pipeline.text_utils import clean_text
from app.pipeline.types import ParsedUnit, unit_warning

log = logging.getLogger(__name__)

WHITE_RGB: Final = (255, 255, 255)
UINT16_TO_UINT8: Final = 257  # 65535 / 255
_SIXTEEN_BIT_MODES: Final = frozenset({"I;16", "I;16L", "I;16B", "I;16N"})
_ALPHA_MODES: Final = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})


@contextmanager
def open_image(path: Path, settings: PipelineSettings) -> Iterator[Image.Image]:
    """Open an untrusted image safely.

    Pillow only raises DecompressionBombError above 2x MAX_IMAGE_PIXELS (it
    merely warns between 1x and 2x), so the warning is escalated and every
    frame's size is also checked explicitly in `iter_frames`.
    """
    Image.MAX_IMAGE_PIXELS = settings.max_image_pixels
    with py_warnings.catch_warnings():
        py_warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(path) as probe:
                _check_pixels(probe, settings)
                probe.verify()  # structural check; the image is unusable afterwards
            image = Image.open(path)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise PermanentError(ErrorCode.DECOMPRESSION_BOMB, details={"error": str(exc)}) from exc
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise PermanentError(ErrorCode.CORRUPT_FILE, details={"error": str(exc)}) from exc
        try:
            yield image
        finally:
            image.close()


def _check_pixels(image: Image.Image, settings: PipelineSettings) -> None:
    width, height = image.size
    if width * height > settings.max_image_pixels:
        raise PermanentError(
            ErrorCode.DECOMPRESSION_BOMB,
            details={"width": width, "height": height, "limit": settings.max_image_pixels},
        )


def count_frames(path: Path, settings: PipelineSettings) -> int:
    """Validate the image and return its frame count (TIFF may have many)."""
    with open_image(path, settings) as image:
        frames = getattr(image, "n_frames", 1)
    if frames > settings.max_pages:
        raise PermanentError(
            ErrorCode.TOO_MANY_PAGES, details={"frames": frames, "max_pages": settings.max_pages}
        )
    return frames


def to_gray8(image: Image.Image) -> np.ndarray:
    """Any mode (CMYK, palette, alpha, 1-bit, 16-bit, float) -> 8-bit grayscale."""
    if image.mode in _SIXTEEN_BIT_MODES:
        return (np.asarray(image, dtype=np.uint16) // UINT16_TO_UINT8).astype(np.uint8)
    if image.mode in ("I", "F"):
        arr = np.asarray(image, dtype=np.float64)
        lo, hi = float(arr.min()), float(arr.max())
        scaled = (arr - lo) / (hi - lo) * 255 if hi > lo else np.zeros_like(arr)
        return scaled.astype(np.uint8)
    if image.mode == "P" and "transparency" in image.info:
        image = image.convert("RGBA")
    if image.mode in _ALPHA_MODES:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (*WHITE_RGB, 255))
        image = Image.alpha_composite(background, rgba)
    return np.asarray(image.convert("L"), dtype=np.uint8).copy()


def _frame_dpi(image: Image.Image) -> float | None:
    dpi = image.info.get("dpi")
    if not dpi:
        return None
    try:
        value = float(dpi[0])
    except (TypeError, ValueError, IndexError):
        return None
    return value if value > 1 else None


def parse_image_frames(
    path: Path,
    start: int,
    end: int,
    *,
    engine: OcrEngine,
    settings: PipelineSettings,
) -> list[ParsedUnit]:
    """OCR frames [start, end). A failing frame becomes a failed unit."""
    units: list[ParsedUnit] = []
    with open_image(path, settings) as image:
        total = getattr(image, "n_frames", 1)
        for index in range(start, min(end, total)):
            units.append(_parse_frame_safely(image, index, engine, settings))
    return units


def _parse_frame_safely(
    image: Image.Image, index: int, engine: OcrEngine, settings: PipelineSettings
) -> ParsedUnit:
    started = time.perf_counter()
    try:
        unit = _parse_frame(image, index, engine, settings)
    except PipelineError as exc:
        unit = _failed_unit(index, exc.code, exc.message)
    except (OSError, ValueError, SyntaxError) as exc:  # truncated / damaged frame data
        unit = _failed_unit(index, ErrorCode.CORRUPT_FILE, str(exc))
    except Exception as exc:  # never let one frame take the batch down
        log.exception("frame parse failed", extra={"unit_index": index, "stage": "parse"})
        unit = _failed_unit(index, ErrorCode.INTERNAL_ERROR, type(exc).__name__)
    unit.duration_ms = int((time.perf_counter() - started) * 1000)
    return unit


def _parse_frame(image: Image.Image, index: int, engine: OcrEngine, settings: PipelineSettings) -> ParsedUnit:
    image.seek(index)
    _check_pixels(image, settings)
    frame = image.copy()
    dpi = _frame_dpi(frame)
    original_size = frame.size
    frame = ImageOps.exif_transpose(frame) or frame
    gray = to_gray8(frame)
    del frame
    outcome = ocr_page_image(gray, dpi=dpi, engine=engine, settings=settings)
    cleaned = clean_text(outcome.result.text, settings.max_unit_text_chars)
    image_quality: dict[str, Any] = {
        **outcome.image_quality,
        "source_mode": image.mode,
        "exif_transposed": tuple(gray.shape[::-1]) != original_size,
    }
    structured = (
        {
            "lines": [line.to_dict() for line in outcome.result.lines],
            "coord_space": "image_pixels",
            "image_size": [gray.shape[1], gray.shape[0]],
        }
        if settings.store_layout
        else None
    )
    return ParsedUnit(
        unit_type=UnitType.IMAGE_FRAME,
        unit_index=index,
        extraction_method=ExtractionMethod.OCR,
        text=cleaned.text,
        ocr_confidence=outcome.result.mean_confidence,
        structured_data=structured,
        image_quality=image_quality,
        warnings=cleaned.warnings,
    )


def _failed_unit(index: int, code: ErrorCode, reason: str) -> ParsedUnit:
    return ParsedUnit(
        unit_type=UnitType.IMAGE_FRAME,
        unit_index=index,
        extraction_method=ExtractionMethod.OCR,
        error_code=code,
        warnings=[unit_warning("UNIT_FAILED", reason, error_code=code.value)],
    )
