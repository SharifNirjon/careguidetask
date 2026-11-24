"""Preprocess -> OCR -> fallback, for one page image. Shared by PDF and image parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from app.config import PipelineSettings
from app.pipeline.errors import OcrTimeoutError
from app.pipeline.ocr.base import OcrEngine, OcrResult
from app.pipeline.preprocess import Transform, detect_upright, prepare

VARIANT_ENHANCED = "enhanced"
VARIANT_BASE = "base"


@dataclass(slots=True)
class OcrOutcome:
    result: OcrResult  # bboxes already mapped back to input-image pixels
    image_quality: dict[str, Any]


def _recognize(engine: OcrEngine, image: np.ndarray, *, retry: bool) -> OcrResult:
    """One OCR call; a timeout is retried once (page-level), then propagates."""
    try:
        return engine.recognize(image, retry=retry)
    except OcrTimeoutError:
        return engine.recognize(image, retry=retry)


def _map_back(result: OcrResult, transform: Transform) -> None:
    for line in result.lines:
        line.bbox = transform.to_input(line.bbox)


def ocr_page_image(
    gray: np.ndarray,
    *,
    dpi: float | None,
    engine: OcrEngine,
    settings: PipelineSettings,
) -> OcrOutcome:
    """OCR one grayscale page with at most 2 attempts.

    Attempt 1: the conditionally preprocessed image. If its confidence is below
    OCR_MIN_CONFIDENCE, attempt 2 OCRs the original in the engine's retry mode,
    first turned upright when OCR_ORIENTATION_MODE=on_low_confidence detects a
    rotated page (wrong orientation is the most common cause of a very low
    score). The better-scoring result wins.
    """
    mode = settings.ocr_orientation_mode
    prepared, quality = prepare(gray, dpi, engine, settings, detect_orientation=mode == "always")
    first = _recognize(engine, prepared.enhanced, retry=False)
    first.variant = VARIANT_ENHANCED
    _map_back(first, prepared.transform)
    attempts = [_attempt(first)]

    best, best_orientation = first, prepared.transform.orientation_deg
    if first.score < settings.ocr_min_confidence:
        base, base_transform = prepared.base, prepared.base_transform
        if mode == "on_low_confidence":
            upright, degrees = detect_upright(gray, engine, settings)
            if degrees:
                base, base_transform = upright, Transform(orientation_deg=degrees)
                prepared.steps.append(f"orientation:{degrees}")
        second = _recognize(engine, base, retry=True)
        second.variant = VARIANT_BASE
        _map_back(second, base_transform)
        attempts.append(_attempt(second))
        if second.score > first.score:
            best, best_orientation = second, base_transform.orientation_deg

    image_quality = quality.to_dict()
    image_quality.update(
        preprocess_steps=prepared.steps,
        ocr_variant=best.variant,
        ocr_attempts=attempts,
        transform={
            "orientation_deg": best_orientation,
            "deskew_deg": round(prepared.transform.deskew_deg, 2) if best is first else 0.0,
            "note": "bboxes are mapped back through crop/scale; rotations are not inverted",
        },
    )
    return OcrOutcome(best, image_quality)


def _attempt(result: OcrResult) -> dict[str, Any]:
    return {
        "variant": result.variant,
        "engine": result.engine,
        "confidence": result.mean_confidence,
        "duration_ms": result.duration_ms,
    }
