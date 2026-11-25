"""Segment -> prompt text, and merging when a segment is too big for one request.

Normal case: one request per segment with every unit, each behind a marker
line so evidence can cite its unit. Oversized segments (> MAX_PROMPT_CHARS):
  1. HEADER request: first + last unit (header fields live at the top,
     totals at the bottom);
  2. LINE_ITEMS requests: consecutive windows of units that fit the budget.
Results are merged: header fields from (1), line items concatenated from (2)
and de-duplicated (windows never overlap, but repeated rows are common on
multi-page invoices that reprint a carried-over line).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.extraction.prompts import PromptTemplate, Scope
from app.extraction.schemas import InvoiceExtractionLLM, LineItemLLM
from app.llm import ChatMessage
from app.models.enums import ExtractionMethod, UnitType

UNIT_LABELS: Final = {
    UnitType.PDF_PAGE: "page",
    UnitType.IMAGE_FRAME: "image",
    UnitType.SHEET: "sheet",
    UnitType.CSV: "csv",
}
TRUNCATION_NOTE: Final = "[... unit truncated to fit the request ...]"
_DELIMITER = re.compile(r"</?\s*document\s*>", re.I)
_DEDUPE_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class UnitText:
    unit_index: int
    unit_type: UnitType
    text: str
    extraction_method: ExtractionMethod
    ocr_confidence: float | None = None
    sheet_name: str | None = None


@dataclass(frozen=True, slots=True)
class PromptPart:
    scope: Scope
    unit_indexes: tuple[int, ...]
    document: str  # rendered units, ready for the <document> block
    truncated: bool = False


def neutralize_delimiters(text: str) -> str:
    """Document text must not be able to close (or reopen) the <document> block."""
    return _DELIMITER.sub(lambda m: m.group(0).replace("<", "[").replace(">", "]"), text)


def unit_marker(unit: UnitText) -> str:
    label = UNIT_LABELS[unit.unit_type]
    if unit.ocr_confidence is not None:
        source = f"ocr conf {unit.ocr_confidence:.2f}"
    elif unit.sheet_name:
        source = f"sheet {unit.sheet_name!r}"
    else:
        source = unit.extraction_method.value
    return f"=== UNIT {unit.unit_index} ({label}, {source}) ==="


def render_unit(unit: UnitText, max_chars: int | None = None) -> tuple[str, bool]:
    body = neutralize_delimiters(unit.text)
    truncated = max_chars is not None and len(body) > max_chars
    if truncated and max_chars is not None:
        body = f"{body[: max(max_chars - len(TRUNCATION_NOTE) - 1, 0)]}\n{TRUNCATION_NOTE}"
    return f"{unit_marker(unit)}\n{body}", truncated


def _part(scope: Scope, units: list[UnitText], per_unit_cap: int | None = None) -> PromptPart:
    rendered = [render_unit(u, per_unit_cap) for u in units]
    return PromptPart(
        scope=scope,
        unit_indexes=tuple(u.unit_index for u in units),
        document="\n\n".join(text for text, _ in rendered),
        truncated=any(cut for _, cut in rendered),
    )


def plan_parts(units: list[UnitText], max_chars: int) -> list[PromptPart]:
    """One FULL part when it fits; otherwise HEADER + LINE_ITEMS windows."""
    if not units:
        return []
    full = _part(Scope.FULL, units)
    if len(full.document) <= max_chars:
        return [full]
    edges = [units[0], units[-1]] if len(units) > 1 else [units[0]]
    parts = [_part(Scope.HEADER, edges, per_unit_cap=max_chars // len(edges))]
    window: list[UnitText] = []
    size = 0
    for unit in units:
        length = len(render_unit(unit)[0]) + 2
        if window and size + length > max_chars:
            parts.append(_part(Scope.LINE_ITEMS, window, per_unit_cap=max_chars))
            window, size = [], 0
        window.append(unit)
        size += length
    parts.append(_part(Scope.LINE_ITEMS, window, per_unit_cap=max_chars))
    return parts


def messages_for(part: PromptPart, template: PromptTemplate) -> list[ChatMessage]:
    return [
        ChatMessage("system", template.system),
        ChatMessage("user", template.render_user(part.scope, part.document)),
    ]


def _line_key(item: LineItemLLM) -> tuple[str, str, str]:
    def norm(value: str | None) -> str:
        return _DEDUPE_SPACE.sub(" ", (value or "").casefold()).strip()

    return norm(item.description.raw_value), norm(item.quantity.raw_value), norm(item.amount.raw_value)


def merge_parts(results: list[tuple[PromptPart, InvoiceExtractionLLM]]) -> InvoiceExtractionLLM:
    """Header fields from the FULL/HEADER result; line items from every window, deduped."""
    header = next(r for p, r in results if p.scope in (Scope.FULL, Scope.HEADER))
    if len(results) == 1:
        return header
    items: list[LineItemLLM] = []
    seen: set[tuple[str, str, str]] = set()
    for part, result in results:
        if part.scope is not Scope.LINE_ITEMS:
            continue
        for item in result.line_items:
            key = _line_key(item)
            if key not in seen:
                seen.add(key)
                items.append(item)
    uncertainties = list(dict.fromkeys(u for _, r in results for u in r.uncertainties))
    return header.model_copy(update={"line_items": items, "uncertainties": uncertainties})
