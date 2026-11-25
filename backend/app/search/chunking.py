"""Document units -> search chunks. Pure: no DB, no clock, no network.

Token estimate: max(ceil(chars / 4), ceil(words * 1.3)). Roughly 4 chars per
token holds for English prose with BPE tokenizers; the word term catches
number- and code-heavy invoice text, where tokens are shorter. It errs high,
so a chunk never exceeds the model's real budget, and needs no tokenizer
dependency. Every size check estimates the ASSEMBLED chunk text, so the
cap holds after joining too (sums of per-line estimates would not).

Text units are packed line by line with overlap; a line is split only when it
alone exceeds the budget (at word boundaries; at characters for a single giant
"word"). Sheet/CSV units are packed by rows, and every chunk starts with the
sheet title and the header row, so each chunk is self-describing.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from app.models.enums import ChunkKind, UnitType

CHARS_PER_TOKEN: Final = 4
TOKENS_PER_WORD: Final = 1.3
TABULAR_UNITS: Final = frozenset({UnitType.SHEET, UnitType.CSV})
SUMMARY_ITEM_LIMIT: Final = 12
CELL_SEPARATOR: Final = " | "
_WORDS: Final = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    if not text.strip():
        return 0
    words = len(_WORDS.findall(text))
    return max(math.ceil(len(text) / CHARS_PER_TOKEN), math.ceil(words * TOKENS_PER_WORD))


@dataclass(frozen=True, slots=True)
class UnitInput:
    unit_index: int
    unit_type: UnitType
    text: str | None
    sheet_name: str | None = None
    structured_data: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    kind: ChunkKind
    unit_index: int
    content: str
    token_count: int


@dataclass(frozen=True, slots=True)
class LineSummary:
    description: str | None
    amount: Decimal | None


@dataclass(frozen=True, slots=True)
class ExtractionSummaryInput:
    document_type: str
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    customer_name: str | None
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    currency: str | None
    line_items: Sequence[LineSummary] = ()


# ------------------------------------------------------------------ packing


def _split_oversized(line: str, budget: int, prefix: str) -> list[str]:
    """Split one line that exceeds the budget: at words, else at characters."""
    pieces: list[str] = []
    current = ""
    for word in line.split():
        candidate = f"{current} {word}" if current else word
        if _fits(prefix, [candidate], budget):
            current = candidate
            continue
        if current:
            pieces.append(current)
        if _fits(prefix, [word], budget):
            current = word
            continue
        step = _max_chars(prefix, budget)  # a single "word" longer than the budget: cut it
        fragments = [word[i : i + step] for i in range(0, len(word), step)]
        pieces.extend(fragments[:-1])
        current = fragments[-1]
    if current:
        pieces.append(current)
    return pieces


def _max_chars(prefix: str, budget: int) -> int:
    """Longest run of characters that fits next to `prefix`."""
    step = max(1, (budget - estimate_tokens(prefix)) * CHARS_PER_TOKEN)
    while step > 1 and not _fits(prefix, ["x" * step], budget):
        step -= 1
    return step


def cap_text(text: str, max_tokens: int) -> str:
    """Hard cap for text that must fit on its own (a giant header, a summary)."""
    if estimate_tokens(text) <= max_tokens:
        return text
    cut = max_tokens * CHARS_PER_TOKEN
    while cut > 1 and estimate_tokens(text[:cut]) > max_tokens:
        cut -= max(1, cut // 10)
    return text[:cut]


def _join(prefix: str, lines: Sequence[str]) -> str:
    body = "\n".join(lines)
    return f"{prefix}\n{body}" if prefix else body


def _fits(prefix: str, lines: Sequence[str], budget: int) -> bool:
    return estimate_tokens(_join(prefix, lines)) <= budget


def pack_lines(lines: Iterable[str], *, max_tokens: int, overlap_tokens: int, prefix: str = "") -> list[str]:
    """Greedy line packing. Consecutive chunks share up to `overlap_tokens` of
    trailing lines, so a sentence cut at a boundary is still found whole."""
    pieces: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        pieces.extend(
            [line] if _fits(prefix, [line], max_tokens) else _split_oversized(line, max_tokens, prefix)
        )

    chunks: list[list[str]] = []
    current: list[str] = []
    for piece in pieces:
        if current and not _fits(prefix, [*current, piece], max_tokens):
            chunks.append(current)
            current = _overlap(current, overlap_tokens, prefix, piece, max_tokens)
        current.append(piece)
    if current:
        chunks.append(current)
    return [_join(prefix, c) for c in chunks]


def _overlap(previous: list[str], overlap_tokens: int, prefix: str, nxt: str, max_tokens: int) -> list[str]:
    """Trailing lines of the previous chunk (never all of it: the chunk must advance)."""
    kept: list[str] = []
    for line in reversed(previous[1:]):
        candidate = [line, *kept]
        if estimate_tokens("\n".join(candidate)) > overlap_tokens:
            break
        if not _fits(prefix, [*candidate, nxt], max_tokens):
            break
        kept = candidate
    return kept


# ------------------------------------------------------------------- units


def _table_lines(unit: UnitInput) -> list[tuple[str, list[str]]]:
    """[(header line, row lines)] per table block of a sheet/CSV unit."""
    data = unit.structured_data or {}
    rows: list[dict[str, Any]] = data.get("rows") or []
    blocks: list[dict[str, Any]] = data.get("table_blocks") or []
    out = []
    for block in blocks:
        headers: list[str] = block.get("headers") or []
        lines = [
            CELL_SEPARATOR.join(str(r.get(h, "")) for h in headers)
            for r in rows
            if r.get("_block") == block.get("index")
        ]
        out.append((CELL_SEPARATOR.join(headers), [line for line in lines if line.strip(" |")]))
    return out


def chunk_unit(unit: UnitInput, *, max_tokens: int, overlap_tokens: int) -> list[ChunkDraft]:
    if unit.unit_type in TABULAR_UNITS and (unit.structured_data or {}).get("table_blocks"):
        title = f"Sheet: {unit.sheet_name}" if unit.sheet_name else ""
        texts: list[str] = []
        for header, rows in _table_lines(unit):
            # A header may take at most half the budget: rows must still fit beside it.
            prefix = cap_text("\n".join(p for p in (title, header) if p), max_tokens // 2)
            texts.extend(
                pack_lines(rows, max_tokens=max_tokens, overlap_tokens=overlap_tokens, prefix=prefix)
            )
    else:
        texts = pack_lines(
            (unit.text or "").splitlines(), max_tokens=max_tokens, overlap_tokens=overlap_tokens
        )
    return [ChunkDraft(ChunkKind.CONTENT, unit.unit_index, t, estimate_tokens(t)) for t in texts]


# ----------------------------------------------------------------- summary


def _money(value: Decimal) -> str:
    """1234.5600 -> '1,234.56' (minor-unit precision is enough for search text)."""
    return f"{value:,.2f}"


def summary_text(s: ExtractionSummaryInput, *, max_tokens: int) -> str:
    """One sentence of normalized fields, e.g. "Invoice INV-2026-001 from Acme Ltd
    to Beta Corp, dated 2026-03-01 (March 2026), total 1,234.56 USD. Items: ...".

    The month name makes "acme invoices in march" match by keyword as well as
    semantically. Items are appended only while the summary stays in budget.
    """
    kind = s.document_type.replace("_", " ").capitalize() if s.document_type else "Invoice"
    head = [kind, s.invoice_number] if s.invoice_number else [kind]
    parts = [" ".join(head)]
    if s.supplier_name:
        parts.append(f"from {s.supplier_name}")
    if s.customer_name:
        parts.append(f"to {s.customer_name}")
    sentence = " ".join(parts)
    details = []
    if s.invoice_date:
        details.append(f"dated {s.invoice_date.isoformat()} ({s.invoice_date:%B %Y})")
    amounts = [(label, v) for label, v in (("subtotal", s.subtotal), ("tax", s.tax)) if v is not None]
    if s.total is not None:
        details.append(f"total {_money(s.total)}{f' {s.currency}' if s.currency else ''}")
    elif s.currency:
        details.append(f"currency {s.currency}")
    details.extend(f"{label} {_money(v)}" for label, v in amounts)
    text = cap_text(f"{sentence}, {', '.join(details)}." if details else f"{sentence}.", max_tokens)
    items = [i for i in s.line_items if i.description][:SUMMARY_ITEM_LIMIT]
    listed: list[str] = []
    for item in items:
        entry = (
            f"{item.description} ({_money(item.amount)})"
            if item.amount is not None
            else str(item.description)
        )
        candidate = f"{text} Items: {'; '.join([*listed, entry])}."
        if estimate_tokens(candidate) > max_tokens:
            break
        listed.append(entry)
    return f"{text} Items: {'; '.join(listed)}." if listed else text


def summary_chunk(s: ExtractionSummaryInput, unit_index: int, *, max_tokens: int) -> ChunkDraft:
    text = summary_text(s, max_tokens=max_tokens)
    return ChunkDraft(ChunkKind.EXTRACTION_SUMMARY, unit_index, text, estimate_tokens(text))
