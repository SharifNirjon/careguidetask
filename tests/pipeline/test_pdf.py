from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PipelineSettings
from app.models.enums import ErrorCode, ExtractionMethod, UnitType
from app.pipeline import pdf
from app.pipeline.errors import PermanentError
from app.pipeline.ocr.tesseract import TesseractEngine
from tests.fixtures import factory
from tests.pipeline.conftest import FakeOcrEngine


def _parse(path: Path, settings: PipelineSettings, engine=None):
    info = pdf.inspect_pdf(path, settings)
    return pdf.parse_pdf_pages(path, 0, info.page_count, engine=engine or FakeOcrEngine(), settings=settings)


def test_digital_pdf_uses_text_layer_with_layout(
    fixture_files: dict[str, Path], settings: PipelineSettings
) -> None:
    engine = FakeOcrEngine()
    units = _parse(fixture_files["digital_invoice.pdf"], settings, engine)
    assert [u.unit_type for u in units] == [UnitType.PDF_PAGE] * 2
    assert all(u.extraction_method is ExtractionMethod.DIGITAL for u in units)
    assert engine.calls == 0
    assert "INV-2024-001" in units[0].text
    assert units[0].text.splitlines()[0] == "INVOICE"  # line breaks preserved
    lines = units[0].structured_data["lines"]
    assert lines[0]["text"] == "INVOICE" and len(lines[0]["bbox"]) == 4
    assert units[0].structured_data["coord_space"] == "pdf_points"


def test_layout_can_be_disabled(fixture_files: dict[str, Path], tmp_path: Path) -> None:
    settings = PipelineSettings(storage_root=tmp_path, store_layout=False)
    assert _parse(fixture_files["digital_invoice.pdf"], settings)[0].structured_data is None


def test_encrypted_pdf(fixture_files: dict[str, Path], settings: PipelineSettings) -> None:
    with pytest.raises(PermanentError) as info:
        pdf.inspect_pdf(fixture_files["encrypted.pdf"], settings)
    assert info.value.code is ErrorCode.PASSWORD_PROTECTED


def test_garbage_bytes_are_corrupt(tmp_path: Path, settings: PipelineSettings) -> None:
    path = tmp_path / "x.pdf"
    path.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 4)
    with pytest.raises(PermanentError) as info:
        pdf.inspect_pdf(path, settings)
    assert info.value.code is ErrorCode.CORRUPT_FILE


def test_truncated_pdf_is_corrupt(fixture_files: dict[str, Path], settings: PipelineSettings) -> None:
    with pytest.raises(PermanentError) as info:
        pdf.inspect_pdf(fixture_files["truncated.pdf"], settings)
    assert info.value.code is ErrorCode.CORRUPT_FILE


def test_repaired_pdf_is_flagged(fixture_files: dict[str, Path], settings: PipelineSettings) -> None:
    """MuPDF repairs mildly truncated files; the repair is recorded so the
    orchestrator reports CORRUPT_FILE (not NO_TEXT_FOUND) if nothing usable comes out."""
    info = pdf.inspect_pdf(fixture_files["repairable.pdf"], settings)
    assert info.repaired


def test_too_many_pages_checked_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "many.pdf"
    path.write_bytes(factory.digital_pdf([["page"]] * 5))
    with pytest.raises(PermanentError) as info:
        pdf.inspect_pdf(path, PipelineSettings(storage_root=tmp_path, max_pages=4))
    assert info.value.code is ErrorCode.TOO_MANY_PAGES


def test_blank_page_goes_to_ocr(tmp_path: Path, settings: PipelineSettings) -> None:
    path = tmp_path / "blank.pdf"
    path.write_bytes(factory.digital_pdf([[]]))
    engine = FakeOcrEngine("", None)
    unit = _parse(path, settings, engine)[0]
    assert unit.extraction_method is ExtractionMethod.OCR
    assert unit.image_quality["classification"] == "too_little_text"
    assert engine.calls >= 1


def test_render_dpi_is_capped_for_huge_pages(tmp_path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page(width=14_000, height=14_000)  # ~194 x 194 inches
    path = tmp_path / "huge.pdf"
    doc.save(path)
    settings = PipelineSettings(storage_root=tmp_path, max_image_pixels=4_000_000)
    unit = _parse(path, settings)[0]
    assert any(w["code"] == "RENDER_DPI_REDUCED" for w in unit.warnings)
    assert unit.image_quality["width"] * unit.image_quality["height"] <= 4_000_000


@pytest.mark.ocr
def test_scanned_pdf_is_ocred(
    fixture_files: dict[str, Path], settings: PipelineSettings, tesseract: TesseractEngine
) -> None:
    unit = _parse(fixture_files["scanned_invoice.pdf"], settings, tesseract)[0]
    assert unit.extraction_method is ExtractionMethod.OCR
    assert "INVOICE" in unit.text.upper() and "INV-2024-001" in unit.text
    assert 0 < unit.ocr_confidence <= 1
    assert any(s.startswith("deskew") for s in unit.image_quality["preprocess_steps"])
    assert unit.structured_data["lines"]


@pytest.mark.ocr
def test_mixed_pdf_method_per_page(
    fixture_files: dict[str, Path], settings: PipelineSettings, tesseract: TesseractEngine
) -> None:
    units = _parse(fixture_files["mixed_invoice.pdf"], settings, tesseract)
    assert [u.extraction_method for u in units] == [ExtractionMethod.DIGITAL, ExtractionMethod.OCR]
    assert "INV-2024-001" in units[1].text


@pytest.mark.ocr
def test_broken_font_text_layer_falls_back_to_ocr(
    fixture_files: dict[str, Path], settings: PipelineSettings, tesseract: TesseractEngine
) -> None:
    unit = _parse(fixture_files["broken_font.pdf"], settings, tesseract)[0]
    assert unit.extraction_method is ExtractionMethod.OCR
    assert unit.image_quality["classification"] == "garbage_text_layer"
    assert "INV-2024-777" in unit.text
