from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PipelineSettings
from app.pipeline import preprocess
from tests.fixtures import factory
from tests.pipeline.conftest import FakeOcrEngine


@pytest.fixture(scope="module")
def clean_page():
    return factory.render_page_image(factory.invoice_lines("INV-1"), dpi=200)


@pytest.mark.parametrize("angle", [-4.0, -1.5, 3.0, 6.0])
def test_skew_estimate_within_one_degree(clean_page, settings: PipelineSettings, angle: float) -> None:
    rotated = factory.degrade(clean_page, rotate_deg=angle, noise_sigma=0)
    assert abs(preprocess.estimate_skew(rotated, settings.deskew_max_angle_deg) - angle) <= 1.0


def test_clean_image_gets_no_blind_preprocessing(clean_page, settings: PipelineSettings) -> None:
    prepared, quality = preprocess.prepare(clean_page, 200, FakeOcrEngine(), settings)
    assert abs(quality.skew_deg) <= settings.deskew_min_angle_deg
    assert prepared.steps == []
    assert prepared.enhanced is prepared.base


def test_scanned_image_gets_deskewed(clean_page, settings: PipelineSettings) -> None:
    scanned = factory.degrade(clean_page, rotate_deg=3.0, noise_sigma=12)
    prepared, quality = preprocess.prepare(scanned, 200, FakeOcrEngine(), settings)
    assert any(s.startswith("deskew") for s in prepared.steps)
    assert quality.to_dict().keys() >= {"width", "height", "dpi", "blur_score", "contrast", "skew_deg"}


def test_low_dpi_image_is_upscaled(clean_page, tmp_path: Path) -> None:
    settings = PipelineSettings(storage_root=tmp_path)
    prepared, _ = preprocess.prepare(clean_page, 100, FakeOcrEngine(), settings)
    assert any(s.startswith("upscale") for s in prepared.steps)
    assert prepared.transform.scale > 1
    x0, _, x1, _ = prepared.transform.to_input((300.0, 300.0, 600.0, 600.0))
    assert x1 - x0 == pytest.approx(300 / prepared.transform.scale)


def test_dark_scanner_border_is_trimmed(clean_page, settings: PipelineSettings) -> None:
    bordered = clean_page.copy()
    bordered[:, :25] = 0
    trimmed, crop_x, crop_y = preprocess.trim_dark_border(bordered)
    assert crop_x == 25 and crop_y == 0
    assert trimmed.shape[1] == clean_page.shape[1] - 25


@pytest.mark.ocr
def test_upside_down_and_sideways_pages_are_reoriented(clean_page, settings: PipelineSettings) -> None:
    import cv2

    from app.pipeline.ocr.tesseract import TesseractEngine

    engine = TesseractEngine(settings.ocr_langs, settings.ocr_page_timeout_s)
    for code, expected in (
        (cv2.ROTATE_90_COUNTERCLOCKWISE, "orientation:90"),
        (cv2.ROTATE_180, "orientation:180"),
    ):
        prepared, _ = preprocess.prepare(
            cv2.rotate(clean_page, code), 200, engine, settings, detect_orientation=True
        )
        assert expected in prepared.steps
        assert prepared.base.shape == clean_page.shape


@pytest.mark.ocr
def test_upside_down_page_is_fixed_by_the_low_confidence_retry(
    clean_page, settings: PipelineSettings
) -> None:
    """Default mode: no OSD up front. Tesseract copes with sideways text on its own,
    but upside-down text scores low, which triggers OSD for attempt 2."""
    import cv2

    from app.pipeline.ocr.runner import ocr_page_image
    from app.pipeline.ocr.tesseract import TesseractEngine

    engine = TesseractEngine(settings.ocr_langs, settings.ocr_page_timeout_s)
    outcome = ocr_page_image(
        cv2.rotate(clean_page, cv2.ROTATE_180), dpi=200, engine=engine, settings=settings
    )
    assert "INV-1" in outcome.result.text
    assert outcome.image_quality["transform"]["orientation_deg"] == 180
    assert len(outcome.image_quality["ocr_attempts"]) == 2
