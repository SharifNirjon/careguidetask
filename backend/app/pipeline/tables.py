"""Grid -> table structure, shared by spreadsheet and CSV parsing.

Input is a list of rows of raw strings ("" = empty). Output is the
`structured_data` shape documented in docs/pipeline.md:

    {headers, rows: [{<header>: value, "_row": n, "_block": i}], table_blocks,
     hidden, n_rows, n_cols, warnings}
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

# Header detection: within the first rows of a block, the first row that is
# mostly filled and mostly non-numeric is the header. Values chosen on sample
# invoice exports: titles/addresses above a table fill 1-2 cells (< 50%), and
# data rows are dominated by numbers/dates.
HEADER_SCAN_ROWS: Final = 20
HEADER_MIN_FILLED_RATIO: Final = 0.5
HEADER_MIN_TEXT_RATIO: Final = 0.7
# A block needs at least this many non-empty rows to count as a table.
MIN_BLOCK_ROWS: Final = 1

_NUMERIC_LIKE = re.compile(r"^[\s+\-−(]*[\d.,'\s]+%?\)?$|^\d{4}-\d{2}-\d{2}(T[\d:.]+)?$")

Grid = list[list[str]]


@dataclass(slots=True)
class TableBlock:
    start_row: int  # 1-based source row numbers (spreadsheet coordinates)
    end_row: int
    header_row: int | None
    headers: list[str]
    rows: list[list[str]]
    row_numbers: list[int]

    def metadata(self, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "start_row": self.start_row,
            "end_row": self.end_row,
            "header_row": self.header_row,
            "headers": self.headers,
            "n_rows": len(self.rows),
        }


def is_numeric_like(value: str) -> bool:
    return bool(_NUMERIC_LIKE.match(value))


def unique_headers(raw: list[str]) -> list[str]:
    """Make header names usable as dict keys: non-empty and unique."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, name in enumerate(raw, start=1):
        base = name.strip() or f"col_{i}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        out.append(base if count == 0 else f"{base}_{count + 1}")
    return out


def drop_empty_columns(grid: Grid) -> Grid:
    if not grid:
        return grid
    width = max(len(r) for r in grid)
    keep = [c for c in range(width) if any(c < len(r) and r[c] != "" for r in grid)]
    return [[r[c] if c < len(r) else "" for c in keep] for r in grid]


def content_width(row: list[str]) -> int:
    """Index of the last non-empty cell + 1 (trailing blanks don't count)."""
    for i in range(len(row) - 1, -1, -1):
        if row[i] != "":
            return i + 1
    return 0


def detect_header(rows: list[list[str]]) -> int | None:
    """Index (into `rows`) of the header row, or None if no row qualifies."""
    if not rows:
        return None
    width = max(len(r) for r in rows) or 1
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        filled = [v for v in row if v != ""]
        if not filled or len(filled) / width < HEADER_MIN_FILLED_RATIO:
            continue
        if len(filled) > 1 and len(set(filled)) == 1:
            continue  # a forward-filled horizontal merge (title banner), not a header
        text_cells = sum(1 for v in filled if not is_numeric_like(v))
        if text_cells / len(filled) >= HEADER_MIN_TEXT_RATIO:
            return i
    return None


def split_blocks(grid: Grid, row_numbers: list[int]) -> list[TableBlock]:
    """Split on fully blank rows; detect a header inside each block."""
    blocks: list[TableBlock] = []
    current: list[tuple[int, list[str]]] = []

    def flush() -> None:
        if len(current) < MIN_BLOCK_ROWS:
            current.clear()
            return
        # Each block is as wide as its own content, not the widest table on the sheet.
        width = max(content_width(r) for _, r in current)
        rows = [r[:width] + [""] * (width - len(r[:width])) for _, r in current]
        nums = [n for n, _ in current]
        h = detect_header(rows)
        if h is None:
            headers = unique_headers([""] * width)
            data, data_nums, header_row = rows, nums, None
        else:
            headers = unique_headers(rows[h] + [""] * (width - len(rows[h])))
            data, data_nums, header_row = rows[h + 1 :], nums[h + 1 :], nums[h]
            # Rows above the header (titles, addresses) stay as data rows so
            # nothing is lost; they are rare and Step 4 can use them.
            data = rows[:h] + data
            data_nums = nums[:h] + data_nums
        blocks.append(TableBlock(nums[0], nums[-1], header_row, headers, data, data_nums))
        current.clear()

    for num, row in zip(row_numbers, grid, strict=True):
        if all(v == "" for v in row):
            flush()
        else:
            current.append((num, row))
    flush()
    return blocks


def build_structured(
    grid: Grid,
    row_numbers: list[int],
    *,
    hidden: bool,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the structured_data dict for a sheet or CSV unit."""
    grid = drop_empty_columns(grid)
    blocks = split_blocks(grid, row_numbers)
    primary = max(blocks, key=lambda b: len(b.rows), default=None)
    rows: list[dict[str, Any]] = []
    for bi, block in enumerate(blocks):
        for num, values in zip(block.row_numbers, block.rows, strict=True):
            record: dict[str, Any] = {
                h: (values[i] if i < len(values) else "") for i, h in enumerate(block.headers)
            }
            record["_row"] = num
            record["_block"] = bi
            rows.append(record)
    return {
        "headers": primary.headers if primary else [],
        "rows": rows,
        "table_blocks": [b.metadata(i) for i, b in enumerate(blocks)],
        "primary_block": blocks.index(primary) if primary else None,
        "hidden": hidden,
        "n_rows": len(rows),
        "n_cols": max((len(b.headers) for b in blocks), default=0),
        "warnings": warnings,
    }


def render_text(title: str, structured: dict[str, Any]) -> str:
    """Compact pipe-delimited rendering for the LLM and full-text search."""
    lines = [title]
    rows = structured["rows"]
    for block in structured["table_blocks"]:
        headers: list[str] = block["headers"]
        lines.append("")
        lines.append(" | ".join(headers))
        for record in rows:
            if record["_block"] == block["index"]:
                lines.append(" | ".join(str(record.get(h, "")) for h in headers))
    return "\n".join(lines)
