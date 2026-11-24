"""OCR engines behind a common protocol."""

from app.pipeline.ocr.base import OcrEngine, OcrResult, Orientation

__all__ = ["OcrEngine", "OcrResult", "Orientation"]
