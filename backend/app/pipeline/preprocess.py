"""Image quality measurement and CONDITIONAL preprocessing for OCR.

Measure first, then apply only the steps the metrics call for: blind
preprocessing (always binarize, always denoise) measurably hurts clean images.
All images are 8-bit single-channel numpy arrays.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Final

import cv2
import numpy as np

from app.config import PipelineSettings
from app.pipeline.ocr.base import OcrEngine

# Skew search works on a downscaled copy; this is plenty for ±0.1° precision.
SKEW_WORK_MAX_SIDE: Final = 1200
SKEW_COARSE_STEP_DEG: Final = 0.5
SKEW_FINE_STEP_DEG: Final = 0.1
# Border trim: an edge row/column this dark is scanner background, not paper.
DARK_BORDER_MEAN: Final = 90
MAX_BORDER_TRIM_RATIO: Final = 0.1
BACKGROUND_BLUR_KERNEL: Final = 51
ADAPTIVE_BLOCK_SIZE: Final = 31
ADAPTIVE_C: Final = 15
MIN_COMPONENTS_FOR_STATS: Final = 10
WHITE: Final = 255


@dataclass(slots=True)
class ImageQuality:
    width: int
    height: int
    dpi: float | None
    blur_score: float  # variance of Laplacian; low = blurry
    contrast: float  # mean background - mean ink (Otsu split); high = crisp
    illumination_unevenness: float  # std of the blurred background
    speck_ratio: float  # share of connected components that are tiny specks
    skew_deg: float  # positive = text rotated counter-clockwise
    text_height_px: float | None  # median glyph height

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass(slots=True)
class Transform:
    """How preprocessed coordinates map back to the input image."""

    orientation_deg: int = 0
    crop_x: int = 0
    crop_y: int = 0
    deskew_deg: float = 0.0
    scale: float = 1.0

    def to_input(self, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """Undo scale + crop exactly. Rotations are recorded, not inverted."""
        x0, y0, x1, y1 = (v / self.scale for v in bbox)
        return (x0 + self.crop_x, y0 + self.crop_y, x1 + self.crop_x, y1 + self.crop_y)


@dataclass(slots=True)
class Prepared:
    base: np.ndarray  # orientation-corrected only (fallback variant)
    enhanced: np.ndarray  # base + the conditional steps
    steps: list[str] = field(default_factory=list)
    transform: Transform = field(default_factory=Transform)
    base_transform: Transform = field(default_factory=Transform)


# ------------------------------------------------------------------ metrics


def _binary_inv(gray: np.ndarray) -> np.ndarray:
    _, binary = cv2.threshold(gray, 0, WHITE, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return binary


def _component_stats(binary: np.ndarray, speck_max_area: int) -> tuple[float, float | None]:
    n, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if n <= 1:
        return 0.0, None
    areas = stats[1:, cv2.CC_STAT_AREA]
    heights = stats[1:, cv2.CC_STAT_HEIGHT]
    specks = areas <= speck_max_area
    speck_ratio = float(specks.mean())
    glyph_heights = heights[~specks]
    if len(glyph_heights) < MIN_COMPONENTS_FOR_STATS:
        return speck_ratio, None
    return speck_ratio, float(np.median(glyph_heights))


def _rotate(gray: np.ndarray, angle_deg: float, border: int = WHITE) -> np.ndarray:
    """Rotate counter-clockwise by angle_deg around the centre, keeping size."""
    h, w = gray.shape
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    return cv2.warpAffine(
        gray, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=border
    )


def _downscale(gray: np.ndarray, max_side: int) -> np.ndarray:
    h, w = gray.shape
    scale = max_side / max(h, w)
    if scale >= 1:
        return gray
    return cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)


def estimate_skew(gray: np.ndarray, max_angle_deg: float) -> float:
    """Skew angle in degrees (positive = text rotated counter-clockwise).

    Projection-profile search: the rotation that makes text rows most
    'peaky' (max variance of row sums) is the correction; skew is its negative.
    """
    binary = _binary_inv(_downscale(gray, SKEW_WORK_MAX_SIDE))
    if not binary.any():
        return 0.0

    def score(angle: float) -> float:
        rotated = _rotate(binary, angle, border=0)
        return float(np.var(rotated.sum(axis=1, dtype=np.float64)))

    coarse = np.arange(-max_angle_deg, max_angle_deg + 1e-9, SKEW_COARSE_STEP_DEG)
    best = max(coarse, key=score)
    fine = np.arange(best - SKEW_COARSE_STEP_DEG, best + SKEW_COARSE_STEP_DEG + 1e-9, SKEW_FINE_STEP_DEG)
    correction = float(max(fine, key=score))
    return -correction


def ink_contrast(gray: np.ndarray, ink_mask: np.ndarray) -> float:
    """Brightness gap between paper and ink. Plain std is misleading on documents:
    a crisp page that is 95% white has a low std."""
    ink = ink_mask > 0
    if not ink.any() or ink.all():
        return 0.0
    return float(gray[~ink].mean() - gray[ink].mean())


def measure_quality(gray: np.ndarray, dpi: float | None, settings: PipelineSettings) -> ImageQuality:
    h, w = gray.shape
    background = cv2.GaussianBlur(
        _downscale(gray, SKEW_WORK_MAX_SIDE), (BACKGROUND_BLUR_KERNEL, BACKGROUND_BLUR_KERNEL), 0
    )
    ink_mask = _binary_inv(gray)
    speck_ratio, text_height = _component_stats(ink_mask, settings.speck_max_area_px)
    return ImageQuality(
        width=w,
        height=h,
        dpi=dpi,
        blur_score=float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        contrast=ink_contrast(gray, ink_mask),
        illumination_unevenness=float(background.std()),
        speck_ratio=speck_ratio,
        skew_deg=estimate_skew(gray, settings.deskew_max_angle_deg),
        text_height_px=text_height,
    )


# -------------------------------------------------------------------- steps


def rotate_orientation(gray: np.ndarray, clockwise_deg: int) -> np.ndarray:
    codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    return cv2.rotate(gray, codes[clockwise_deg]) if clockwise_deg in codes else gray


def trim_dark_border(gray: np.ndarray) -> tuple[np.ndarray, int, int]:
    """Cut dark scanner edges. Returns (image, crop_x, crop_y)."""
    h, w = gray.shape
    max_y, max_x = int(h * MAX_BORDER_TRIM_RATIO), int(w * MAX_BORDER_TRIM_RATIO)
    row_mean, col_mean = gray.mean(axis=1), gray.mean(axis=0)

    def run(values: np.ndarray, limit: int) -> int:
        n = 0
        while n < limit and values[n] < DARK_BORDER_MEAN:
            n += 1
        return n

    top, bottom = run(row_mean, max_y), run(row_mean[::-1], max_y)
    left, right = run(col_mean, max_x), run(col_mean[::-1], max_x)
    if not (top or bottom or left or right):
        return gray, 0, 0
    return gray[top : h - bottom, left : w - right], left, top


def upscale_factor(q: ImageQuality, settings: PipelineSettings) -> float:
    factors = [1.0]
    if q.dpi and q.dpi < settings.min_effective_dpi:
        factors.append(settings.ocr_dpi / q.dpi)
    if q.text_height_px and q.text_height_px < settings.min_text_height_px:
        factors.append(settings.min_text_height_px / q.text_height_px)
    factor = min(max(factors), settings.max_upscale_factor)
    pixel_cap = (settings.max_image_pixels / max(1, q.width * q.height)) ** 0.5
    return max(1.0, min(factor, pixel_cap))


def binarize(gray: np.ndarray, adaptive: bool) -> np.ndarray:
    if adaptive:
        return cv2.adaptiveThreshold(
            gray, WHITE, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, ADAPTIVE_BLOCK_SIZE, ADAPTIVE_C
        )
    _, out = cv2.threshold(gray, 0, WHITE, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return out


def detect_upright(gray: np.ndarray, engine: OcrEngine, settings: PipelineSettings) -> tuple[np.ndarray, int]:
    """Rotate to upright using the engine's orientation detection. Returns (image, degrees)."""
    orientation = engine.detect_orientation(gray)
    if orientation and orientation.rotate_deg and orientation.confidence >= settings.ocr_min_osd_confidence:
        return rotate_orientation(gray, orientation.rotate_deg), orientation.rotate_deg
    return gray, 0


def prepare(
    gray: np.ndarray,
    dpi: float | None,
    engine: OcrEngine,
    settings: PipelineSettings,
    *,
    detect_orientation: bool = False,
) -> tuple[Prepared, ImageQuality]:
    """(Optionally) fix orientation, measure quality, then apply only the steps it calls for."""
    steps: list[str] = []
    transform = Transform()

    if detect_orientation:
        gray, degrees = detect_upright(gray, engine, settings)
        if degrees:
            transform.orientation_deg = degrees
            steps.append(f"orientation:{degrees}")
    base = gray
    base_transform = Transform(orientation_deg=transform.orientation_deg)
    quality = measure_quality(gray, dpi, settings)  # on the upright image

    img, crop_x, crop_y = trim_dark_border(gray)
    if crop_x or crop_y or img.shape != gray.shape:
        transform.crop_x, transform.crop_y = crop_x, crop_y
        steps.append("trim_border")

    if abs(quality.skew_deg) > settings.deskew_min_angle_deg:
        img = _rotate(img, -quality.skew_deg)
        transform.deskew_deg = -quality.skew_deg
        steps.append(f"deskew:{-quality.skew_deg:.1f}")

    noisy = quality.speck_ratio > settings.noise_speck_ratio
    if noisy:
        img = cv2.medianBlur(img, 3)
        steps.append("denoise")

    factor = upscale_factor(quality, settings)
    if factor > 1.0:
        img = cv2.resize(img, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        transform.scale = factor
        steps.append(f"upscale:{factor:.2f}")

    uneven = quality.illumination_unevenness > settings.uneven_illumination_threshold
    if uneven or noisy or quality.contrast < settings.low_contrast_threshold:
        img = binarize(img, adaptive=uneven)
        steps.append("adaptive_threshold" if uneven else "otsu")

    prepared = Prepared(
        base=base, enhanced=img, steps=steps, transform=transform, base_transform=base_transform
    )
    return prepared, quality
