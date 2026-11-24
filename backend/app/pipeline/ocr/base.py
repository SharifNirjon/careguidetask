"""OCR engine protocol. Engines are swappable (Tesseract today; PaddleOCR could
be added behind the same protocol without touching the parsers)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from app.pipeline.types import OcrLine


@dataclass(slots=True)
class OcrResult:
    text: str
    mean_confidence: float | None  # 0..1, None when no words were recognized
    lines: list[OcrLine] = field(default_factory=list)
    engine: str = ""
    variant: str = ""
    duration_ms: int = 0

    @property
    def score(self) -> float:
        """Ranking key when choosing between OCR attempts."""
        return self.mean_confidence or 0.0


@dataclass(frozen=True, slots=True)
class Orientation:
    rotate_deg: int  # clockwise rotation that makes the page upright: 0/90/180/270
    confidence: float


class OcrEngine(Protocol):
    name: str

    def recognize(self, image: np.ndarray, *, retry: bool = False) -> OcrResult:
        """OCR an 8-bit grayscale image.

        `retry=True` asks for the engine's alternative layout mode (Tesseract:
        psm 6, a single uniform block). Raises OcrTimeoutError / OcrEngineError.
        """
        ...

    def detect_orientation(self, image: np.ndarray) -> Orientation | None:
        """Page orientation, or None when it cannot be determined."""
        ...
