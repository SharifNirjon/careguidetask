"""Tesseract engine via pytesseract (subprocess, never shell=True).

Run workers with OMP_THREAD_LIMIT=1: we parallelize across processes, and
Tesseract's own OpenMP threads would oversubscribe the CPUs.
"""

from __future__ import annotations

import time
from typing import Any, Final

import cv2
import numpy as np
import pytesseract
from pytesseract import Output, TesseractError

from app.pipeline.errors import OcrEngineError, OcrTimeoutError
from app.pipeline.ocr.base import OcrResult, Orientation
from app.pipeline.types import OcrLine

OEM_LSTM: Final = 1
PSM_AUTO: Final = 3  # fully automatic page segmentation
PSM_SINGLE_BLOCK: Final = 6  # assume one uniform block of text (retry mode)
NO_CONFIDENCE: Final = -1
# OSD only needs the page layout, not glyph detail: at full 300 DPI it costs
# about 2x a full OCR pass; at this size it is a fraction of that.
OSD_MAX_SIDE: Final = 1200


class TesseractEngine:
    name = "tesseract"

    def __init__(self, langs: str, timeout_s: int) -> None:
        self.langs = langs
        self.timeout_s = timeout_s

    def recognize(self, image: np.ndarray, *, retry: bool = False) -> OcrResult:
        psm = PSM_SINGLE_BLOCK if retry else PSM_AUTO
        started = time.perf_counter()
        try:
            data = pytesseract.image_to_data(
                image,
                lang=self.langs,
                config=f"--oem {OEM_LSTM} --psm {psm}",
                timeout=self.timeout_s,
                output_type=Output.DICT,
            )
        except RuntimeError as exc:  # pytesseract signals timeouts with RuntimeError
            if "timeout" in str(exc).lower():
                raise OcrTimeoutError(self.timeout_s) from exc
            raise OcrEngineError(str(exc)) from exc
        except TesseractError as exc:
            raise OcrEngineError(str(exc)) from exc
        lines, confidence = _assemble(data)
        return OcrResult(
            text=_join_lines(data, lines),
            mean_confidence=confidence,
            lines=[line for _, line in lines],
            engine=f"{self.name}:psm{psm}",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def detect_orientation(self, image: np.ndarray) -> Orientation | None:
        h, w = image.shape[:2]
        scale = OSD_MAX_SIDE / max(h, w)
        small = (
            cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            if scale < 1
            else image
        )
        try:
            osd = pytesseract.image_to_osd(
                small, config="--psm 0", timeout=self.timeout_s, output_type=Output.DICT
            )
        except (TesseractError, RuntimeError):
            return None  # too little text for OSD; leave the page as is
        return Orientation(int(osd.get("rotate", 0)) % 360, float(osd.get("orientation_conf", 0.0)))


LineKey = tuple[int, int, int]


def _assemble(data: dict[str, list[Any]]) -> tuple[list[tuple[LineKey, OcrLine]], float | None]:
    """Group words into lines; confidence = mean word confidence weighted by length."""
    words: dict[LineKey, list[int]] = {}
    weighted = 0.0
    total_len = 0
    for i, raw in enumerate(data["text"]):
        word = raw.strip()
        conf = float(data["conf"][i])
        if not word or conf == NO_CONFIDENCE:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        words.setdefault(key, []).append(i)
        weighted += conf * len(word)
        total_len += len(word)

    lines: list[tuple[LineKey, OcrLine]] = []
    for key, idxs in words.items():
        x0 = min(data["left"][i] for i in idxs)
        y0 = min(data["top"][i] for i in idxs)
        x1 = max(data["left"][i] + data["width"][i] for i in idxs)
        y1 = max(data["top"][i] + data["height"][i] for i in idxs)
        text = " ".join(data["text"][i].strip() for i in idxs)
        conf = sum(float(data["conf"][i]) for i in idxs) / len(idxs) / 100
        lines.append((key, OcrLine(text, (x0, y0, x1, y1), conf)))
    confidence = (weighted / total_len / 100) if total_len else None
    return lines, confidence


def _join_lines(data: dict[str, list[Any]], lines: list[tuple[LineKey, OcrLine]]) -> str:
    """Line breaks between lines, a blank line between Tesseract blocks."""
    out: list[str] = []
    previous_block: int | None = None
    for (block, _, _), line in lines:
        if previous_block is not None and block != previous_block:
            out.append("")
        out.append(line.text)
        previous_block = block
    return "\n".join(out)
