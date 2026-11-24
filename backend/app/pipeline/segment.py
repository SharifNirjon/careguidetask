"""Invoice boundary detection (where does one invoice end and the next begin).

`Segmenter` is a protocol so Step 4 can add an LLM-assisted segmenter. The
heuristic one scores boundary signals between consecutive pages:

  page counter resets ("Page 1 of N", "1/N")    strong start
  invoice number changes                        strong start
  same invoice number repeated                  "not a boundary"
  page counter continues ("Page 2 of N")        strong "not a boundary"
  invoice title near the top of the page        weak start
  previous page closed with a totals block      weak start

A boundary is cut when the evidence reaches BOUNDARY_THRESHOLD. With no
signals at all, the whole document is one low-confidence segment.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Final, Protocol

from app.models.enums import UnitType

TOP_OF_PAGE_LINES: Final = 12
BOTTOM_OF_PAGE_LINES: Final = 15

W_COUNTER_RESET: Final = 0.6
W_NUMBER_CHANGE: Final = 0.6
W_TITLE_TOP: Final = 0.25
W_PREV_TOTALS: Final = 0.25
W_COUNTER_CONTINUES: Final = -1.0
W_SAME_NUMBER: Final = -0.5
BOUNDARY_THRESHOLD: Final = 0.45

CONF_NO_SIGNALS: Final = 0.2
CONF_BASE: Final = 0.5
CONF_PAGE_COUNT_MATCH: Final = 0.3
CONF_HAS_TOTALS: Final = 0.1
CONF_HAS_NUMBER: Final = 0.1

_COUNTER = re.compile(r"\bpage\s*(\d{1,4})\s*(?:of|/)\s*(\d{1,4})\b", re.I)
_BARE_COUNTER = re.compile(r"^\s*(\d{1,4})\s*/\s*(\d{1,4})\s*$", re.M)
_TITLE = re.compile(r"^\s*(tax\s+invoice|invoice|bill|credit\s+note|receipt)\b", re.I)
_INVOICE_NO = re.compile(
    r"\b(?:invoice|inv|bill|receipt)\s*(?:(?:no\.?|number|num|#)\s*[:#.]?|[:#])\s*"
    r"([A-Z0-9][A-Z0-9\-/._]{2,30})",
    re.I,
)
_TOTALS = re.compile(r"\b(grand\s+total|total\s+due|amount\s+due|balance\s+due|total)\b", re.I)


@dataclass(slots=True)
class Segment:
    unit_start: int
    unit_end: int  # inclusive
    confidence: float
    reasons: list[str] = field(default_factory=list)
    invoice_number: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Segmenter(Protocol):
    def segment(self, texts: Sequence[str | None]) -> list[Segment]:
        """Split a document's unit texts (in unit order) into invoices."""
        ...


@dataclass(slots=True)
class PageSignals:
    counter: tuple[int, int] | None
    title_at_top: bool
    invoice_number: str | None
    totals_at_bottom: bool

    @property
    def any(self) -> bool:
        return bool(self.counter or self.title_at_top or self.invoice_number or self.totals_at_bottom)


def page_signals(text: str | None) -> PageSignals:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    joined = "\n".join(lines)
    counter = None
    if m := (_COUNTER.search(joined) or _BARE_COUNTER.search(joined)):
        current, total = int(m.group(1)), int(m.group(2))
        if 1 <= current <= total:
            counter = (current, total)
    number = next(
        (
            m.group(1).upper().rstrip(".")
            for m in _INVOICE_NO.finditer(joined)
            if any(ch.isdigit() for ch in m.group(1))
        ),
        None,
    )
    return PageSignals(
        counter=counter,
        title_at_top=any(_TITLE.match(ln) for ln in lines[:TOP_OF_PAGE_LINES]),
        invoice_number=number,
        totals_at_bottom=any(_TOTALS.search(ln) for ln in lines[-BOTTOM_OF_PAGE_LINES:]),
    )


class HeuristicSegmenter:
    """Rule-based boundaries; deterministic and explainable (reasons per segment)."""

    def segment(self, texts: Sequence[str | None]) -> list[Segment]:
        if not texts:
            return []
        signals = [page_signals(t) for t in texts]
        if not any(s.any for s in signals):
            return [Segment(0, len(texts) - 1, CONF_NO_SIGNALS, ["no_boundary_signals"])]

        segments: list[Segment] = []
        current = Segment(0, 0, 0.0, ["document_start"], signals[0].invoice_number)
        for i in range(1, len(texts)):
            score, reasons = self._boundary_score(signals[i - 1], signals[i], current.invoice_number)
            if score >= BOUNDARY_THRESHOLD:
                segments.append(current)
                current = Segment(i, i, 0.0, reasons, signals[i].invoice_number)
            else:
                current.unit_end = i
                current.invoice_number = current.invoice_number or signals[i].invoice_number
        segments.append(current)
        for seg in segments:
            seg.confidence = self._confidence(seg, signals)
        return segments

    @staticmethod
    def _boundary_score(
        prev: PageSignals, page: PageSignals, current_number: str | None
    ) -> tuple[float, list[str]]:
        score, reasons = 0.0, []
        if page.counter:
            if page.counter[0] == 1:
                score += W_COUNTER_RESET
                reasons.append("page_counter_reset")
            else:
                score += W_COUNTER_CONTINUES
        if page.invoice_number and current_number:
            if page.invoice_number != current_number:
                score += W_NUMBER_CHANGE
                reasons.append("invoice_number_changed")
            else:
                score += W_SAME_NUMBER
        if page.title_at_top:
            score += W_TITLE_TOP
            reasons.append("invoice_title_at_top")
        if prev.totals_at_bottom:
            score += W_PREV_TOTALS
            reasons.append("previous_page_totals")
        return score, reasons

    @staticmethod
    def _confidence(seg: Segment, signals: list[PageSignals]) -> float:
        pages = signals[seg.unit_start : seg.unit_end + 1]
        conf = CONF_BASE
        counters = [p.counter for p in pages if p.counter]
        if counters and counters[0] == (1, len(pages)) and counters[-1][0] == counters[-1][1]:
            conf += CONF_PAGE_COUNT_MATCH
        if pages[-1].totals_at_bottom:
            conf += CONF_HAS_TOTALS
        if seg.invoice_number:
            conf += CONF_HAS_NUMBER
        return round(min(conf, 1.0), 2)


def segment_document(
    unit_type: UnitType, texts: Sequence[str | None], segmenter: Segmenter | None = None
) -> list[Segment]:
    """Spreadsheets/CSV: one segment per sheet (row grouping by invoice number is
    Step 4's job). Pages and image frames go through the segmenter."""
    if unit_type in (UnitType.SHEET, UnitType.CSV):
        return [Segment(i, i, CONF_BASE, ["one_segment_per_sheet"]) for i in range(len(texts))]
    return (segmenter or HeuristicSegmenter()).segment(texts)
