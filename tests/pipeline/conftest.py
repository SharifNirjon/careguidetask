"""Pipeline test fixtures. Inputs are generated (tests/fixtures/factory.py)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.config import PipelineSettings
from app.pipeline.errors import OcrEngineError
from app.pipeline.ocr.base import OcrEngine, OcrResult, Orientation
from app.pipeline.ocr.tesseract import TesseractEngine
from app.pipeline.orchestrator import PipelineContext
from app.pipeline.storage import LocalStorage
from tests.fixtures import factory

HAS_TESSERACT = shutil.which("tesseract") is not None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if HAS_TESSERACT:
        return
    skip = pytest.mark.skip(reason="tesseract not installed (run in the worker image)")
    for item in items:
        if "ocr" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def settings(tmp_path: Path) -> PipelineSettings:
    return PipelineSettings(storage_root=tmp_path / "storage")


@pytest.fixture(scope="session")
def fixture_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    paths = factory.write_all(tmp_path_factory.mktemp("fixtures"))
    return {p.name: p for p in paths}


@pytest.fixture
def tesseract(settings: PipelineSettings) -> TesseractEngine:
    return TesseractEngine(settings.ocr_langs, settings.ocr_page_timeout_s)


class FakeOcrEngine:
    """Deterministic stand-in so non-OCR tests don't need tesseract."""

    name = "fake"

    def __init__(self, text: str = "", confidence: float | None = None) -> None:
        self.text = text
        self.confidence = confidence
        self.calls = 0

    def recognize(self, image: np.ndarray, *, retry: bool = False) -> OcrResult:
        self.calls += 1
        return OcrResult(self.text, self.confidence, engine=self.name)

    def detect_orientation(self, image: np.ndarray) -> Orientation | None:
        return None


class FailingOcrEngine(FakeOcrEngine):
    """Simulates a page that cannot be recognized (fault injection)."""

    def recognize(self, image: np.ndarray, *, retry: bool = False) -> OcrResult:
        self.calls += 1
        raise OcrEngineError("injected failure")


@pytest.fixture
def make_context(committed_engine: Engine, tmp_path: Path) -> Callable[..., PipelineContext]:
    """Build a PipelineContext on the test DB with an injectable OCR engine."""

    def build(engine: OcrEngine | None = None, **overrides: object) -> PipelineContext:
        settings = PipelineSettings(storage_root=tmp_path / "storage", **overrides)
        ocr = engine or (
            TesseractEngine(settings.ocr_langs, settings.ocr_page_timeout_s)
            if HAS_TESSERACT
            else FakeOcrEngine()
        )
        return PipelineContext(
            session_factory=sessionmaker(bind=committed_engine, expire_on_commit=False),
            storage=LocalStorage(settings.storage_root),
            ocr_engine_factory=lambda: ocr,
            settings=settings,
        )

    return build
