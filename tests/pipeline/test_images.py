from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.config import PipelineSettings
from app.models.enums import ErrorCode, ExtractionMethod, UnitType
from app.pipeline import images
from app.pipeline.errors import PermanentError
from app.pipeline.ocr.tesseract import TesseractEngine
from tests.pipeline.conftest import FakeOcrEngine


def _parse(path: Path, settings: PipelineSettings, engine=None):
    frames = images.count_frames(path, settings)
    return images.parse_image_frames(
        path, 0, frames, engine=engine or FakeOcrEngine("x", 0.9), settings=settings
    )


def test_decompression_bomb_rejected_from_header(
    fixture_files: dict[str, Path], settings: PipelineSettings
) -> None:
    with pytest.raises(PermanentError) as info:
        images.count_frames(fixture_files["bomb.png"], settings)
    assert info.value.code is ErrorCode.DECOMPRESSION_BOMB


def test_bomb_between_1x_and_2x_limit_is_still_rejected(tmp_path: Path) -> None:
    """Pillow only raises above 2x MAX_IMAGE_PIXELS; we must catch 1x-2x too."""
    path = tmp_path / "big.png"
    Image.new("L", (1500, 1000), 255).save(path)  # 1.5 MP
    settings = PipelineSettings(storage_root=tmp_path, max_image_pixels=1_000_000)
    with pytest.raises(PermanentError) as info:
        images.count_frames(path, settings)
    assert info.value.code is ErrorCode.DECOMPRESSION_BOMB


def test_truncated_image_is_corrupt(
    fixture_files: dict[str, Path], tmp_path: Path, settings: PipelineSettings
) -> None:
    path = tmp_path / "cut.png"
    path.write_bytes(fixture_files["blurry.png"].read_bytes()[:2000])
    with pytest.raises(PermanentError) as info:
        _parse(path, settings)
    assert info.value.code is ErrorCode.CORRUPT_FILE


def test_tiff_frames_become_units(fixture_files: dict[str, Path], settings: PipelineSettings) -> None:
    units = _parse(fixture_files["three_frames.tiff"], settings)
    assert [(u.unit_type, u.unit_index) for u in units] == [(UnitType.IMAGE_FRAME, i) for i in range(3)]
    assert all(u.extraction_method is ExtractionMethod.OCR for u in units)


def test_exif_orientation_is_applied(fixture_files: dict[str, Path], settings: PipelineSettings) -> None:
    unit = _parse(fixture_files["exif_rotated.jpg"], settings)[0]
    stored = Image.open(fixture_files["exif_rotated.jpg"]).size
    assert unit.image_quality["exif_transposed"] is True
    # upright page is portrait even though the stored pixels are landscape
    assert stored[0] > stored[1]
    assert unit.image_quality["height"] > unit.image_quality["width"]


@pytest.mark.parametrize("mode", ["CMYK", "P", "RGBA", "LA", "I;16", "1", "F"])
def test_any_mode_converts_to_8bit_gray(mode: str) -> None:
    base = Image.new("RGB", (8, 8), (200, 30, 30))
    if mode == "I;16":
        img = Image.fromarray(np.full((8, 8), 40000, dtype=np.uint16))
    elif mode == "F":
        img = Image.fromarray(np.linspace(0, 1, 64, dtype=np.float32).reshape(8, 8))
    else:
        img = base.convert(mode)
    gray = images.to_gray8(img)
    assert gray.dtype == np.uint8 and gray.shape == (8, 8)
    if mode == "I;16":
        assert int(gray[0, 0]) == 40000 // 257


def test_alpha_is_composited_on_white() -> None:
    transparent = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    assert int(images.to_gray8(transparent).min()) == 255


@pytest.mark.ocr
def test_exif_rotated_jpeg_ocr(
    fixture_files: dict[str, Path], settings: PipelineSettings, tesseract: TesseractEngine
) -> None:
    unit = _parse(fixture_files["exif_rotated.jpg"], settings, tesseract)[0]
    assert "INV-2024-042" in unit.text


@pytest.mark.ocr
def test_blurry_png_is_measured_and_still_read(
    fixture_files: dict[str, Path], settings: PipelineSettings, tesseract: TesseractEngine, tmp_path: Path
) -> None:
    from tests.fixtures import factory

    sharp_path = tmp_path / "sharp.png"
    sharp_path.write_bytes(factory.sharp_png())
    blurry = _parse(fixture_files["blurry.png"], settings, tesseract)[0]
    sharp = _parse(sharp_path, settings, tesseract)[0]
    assert blurry.image_quality["blur_score"] < sharp.image_quality["blur_score"] / 2
    assert "INVOICE" in blurry.text.upper()
    assert blurry.image_quality["ocr_variant"] in ("enhanced", "base")


@pytest.mark.ocr
def test_tiff_frames_ocr(
    fixture_files: dict[str, Path], settings: PipelineSettings, tesseract: TesseractEngine
) -> None:
    units = _parse(fixture_files["three_frames.tiff"], settings, tesseract)
    assert [f"INV-30{i}" in u.text for i, u in enumerate(units)] == [True, True, True]
