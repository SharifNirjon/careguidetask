"""Render one page / image frame as PNG for the UI preview.

The same safety limits as parsing apply (open_pdf / open_image), so a preview
request can't be used to sidestep the decompression-bomb and page checks.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Final

import pymupdf
from PIL import Image

from app.config import PipelineSettings
from app.models.enums import ErrorCode
from app.pipeline import images, pdf
from app.pipeline.errors import PermanentError
from app.pipeline.types import FileKind

ASSUMED_IMAGE_DPI: Final = 300  # scans without DPI metadata are usually 300
PDF_POINTS_PER_INCH: Final = 72


def render_png(kind: FileKind, path: Path, index: int, dpi: int, settings: PipelineSettings) -> bytes:
    if kind is FileKind.PDF:
        return _render_pdf_page(path, index, dpi, settings)
    if kind.is_image:
        return _render_image_frame(path, index, dpi, settings)
    raise PermanentError(ErrorCode.UNSUPPORTED_TYPE, details={"reason": "no visual pages"})


def _render_pdf_page(path: Path, index: int, dpi: int, settings: PipelineSettings) -> bytes:
    doc, info = pdf.open_pdf(path, settings)
    try:
        if not 0 <= index < info.page_count:
            raise IndexError(index)
        pixmap = doc[index].get_pixmap(
            matrix=pymupdf.Matrix(dpi / PDF_POINTS_PER_INCH, dpi / PDF_POINTS_PER_INCH)
        )
        return bytes(pixmap.tobytes("png"))
    finally:
        doc.close()


def _render_image_frame(path: Path, index: int, dpi: int, settings: PipelineSettings) -> bytes:
    with images.open_image(path, settings) as image:
        if not 0 <= index < getattr(image, "n_frames", 1):
            raise IndexError(index)
        image.seek(index)
        source_dpi = float((image.info.get("dpi") or (ASSUMED_IMAGE_DPI,))[0] or ASSUMED_IMAGE_DPI)
        frame = image.convert("RGB")
        scale = min(1.0, dpi / source_dpi)  # never upscale a preview
        if scale < 1.0:
            frame = frame.resize(
                (max(1, int(frame.width * scale)), max(1, int(frame.height * scale))),
                Image.Resampling.LANCZOS,
            )
        out = io.BytesIO()
        frame.save(out, format="PNG", optimize=True)
        return out.getvalue()
