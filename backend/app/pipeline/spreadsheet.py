"""XLSX (openpyxl) and XLS (xlrd) parsing. Pure: path in, ParsedUnit list out.

Never evaluates formulas or macros: only cached values are read. Every value
is kept as a raw string ("00123" and "1.234,56" survive untouched); numeric
normalization is Step 4's job. Every sheet, hidden ones included, is a unit.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Final

import openpyxl
import xlrd
from defusedxml import ElementTree as SafeET  # guards against XML entity bombs

from app.config import PipelineSettings
from app.models.enums import ErrorCode, ExtractionMethod, UnitType
from app.pipeline.errors import PermanentError
from app.pipeline.tables import Grid, build_structured, render_text
from app.pipeline.text_utils import cap_text, clean_text
from app.pipeline.types import ParsedUnit, unit_warning

NS_MAIN: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS_PKG_REL: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
HIDDEN_STATES: Final = frozenset({"hidden", "veryHidden"})
MAX_REPORTED_CELLS: Final = 50  # cap on cell refs listed in a warning
XLS_VISIBLE: Final = 0


@dataclass(slots=True)
class SheetData:
    name: str
    hidden: bool
    grid: Grid
    merges: list[tuple[int, int, int, int]]  # 1-based inclusive (r0, c0, r1, c1)
    warnings: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------- cell values


def cell_to_str(value: Any, max_chars: int) -> str:
    """Raw string form of a cell value; dates -> ISO 8601."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    text = str(value)
    if text.isascii() and text.isprintable():
        return text.strip()[:max_chars]
    return clean_text(text, max_chars).text


def forward_fill_merges(grid: Grid, merges: list[tuple[int, int, int, int]]) -> None:
    """Copy each merged range's top-left value into every cell of the range."""
    for r0, c0, r1, c1 in merges:
        if r0 > len(grid):
            continue
        anchor_row = grid[r0 - 1]
        value = anchor_row[c0 - 1] if c0 - 1 < len(anchor_row) else ""
        for r in range(r0, min(r1, len(grid)) + 1):
            row = grid[r - 1]
            if len(row) < c1:
                row.extend([""] * (c1 - len(row)))
            for c in range(c0, c1 + 1):
                row[c - 1] = value


def _sheet_unit(index: int, sheet: SheetData, settings: PipelineSettings) -> ParsedUnit:
    forward_fill_merges(sheet.grid, sheet.merges)
    warnings = sheet.warnings
    if sheet.hidden:
        warnings.append(unit_warning("HIDDEN_SHEET", f"Sheet '{sheet.name}' is hidden"))
    structured = build_structured(
        sheet.grid, list(range(1, len(sheet.grid) + 1)), hidden=sheet.hidden, warnings=warnings
    )
    structured["merged_ranges"] = len(sheet.merges)
    title = f"Sheet: {sheet.name}" + (" (hidden)" if sheet.hidden else "")
    text = cap_text(render_text(title, structured), settings.max_unit_text_chars, warnings)
    return ParsedUnit(
        unit_type=UnitType.SHEET,
        unit_index=index,
        extraction_method=ExtractionMethod.SPREADSHEET,
        sheet_name=sheet.name,
        text=text,
        structured_data=structured,
        warnings=list(warnings),
    )


def _rows_truncated(settings: PipelineSettings) -> dict[str, Any]:
    return unit_warning(
        "ROWS_TRUNCATED",
        f"Only the first {settings.max_sheet_rows} rows were read",
        max_rows=settings.max_sheet_rows,
    )


# ---------------------------------------------------------------------- XLSX


@dataclass(slots=True)
class _XmlScan:
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    uncached_formulas: list[str] = field(default_factory=list)


def _workbook_sheets(zf: zipfile.ZipFile) -> list[tuple[str, bool, str]]:
    """(name, hidden, xml path) per worksheet, in workbook order."""
    workbook = SafeET.fromstring(zf.read("xl/workbook.xml"))
    rels = SafeET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target", "") for r in rels.iter(f"{NS_PKG_REL}Relationship")}
    sheets = []
    for sheet in workbook.iter(f"{NS_MAIN}sheet"):
        target = targets.get(sheet.get(f"{NS_REL}id"), "")
        path = (
            target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        )
        sheets.append((sheet.get("name", ""), sheet.get("state") in HIDDEN_STATES, path))
    return sheets


def _scan_sheet_xml(zf: zipfile.ZipFile, xml_path: str) -> _XmlScan:
    """Stream the sheet XML once for what read-only openpyxl can't give us:
    merged ranges, and formula cells that have no cached value."""
    scan = _XmlScan()
    if xml_path not in zf.namelist():
        return scan
    with zf.open(xml_path) as fh:
        for _, elem in SafeET.iterparse(fh, events=("end",)):
            tag = elem.tag
            if tag == f"{NS_MAIN}c":
                formula = elem.find(f"{NS_MAIN}f")
                cached = elem.find(f"{NS_MAIN}v")
                if formula is not None and (cached is None or not (cached.text or "").strip()):
                    scan.uncached_formulas.append(elem.get("r", "?"))
            elif tag == f"{NS_MAIN}mergeCell":
                ref = elem.get("ref", "")
                if ":" in ref:
                    a, b = ref.split(":")
                    (c0, r0), (c1, r1) = _ref_to_rc(a), _ref_to_rc(b)
                    scan.merges.append((r0, c0, r1, c1))
            elif tag == f"{NS_MAIN}row":
                elem.clear()  # keep memory flat on big sheets
    return scan


def _ref_to_rc(ref: str) -> tuple[int, int]:
    """'B12' -> (col=2, row=12)."""
    col = 0
    i = 0
    while i < len(ref) and ref[i].isalpha():
        col = col * 26 + (ord(ref[i].upper()) - 64)
        i += 1
    return col, int(ref[i:])


def parse_xlsx(path: Path, settings: PipelineSettings) -> list[ParsedUnit]:
    # A file object, not the path: openpyxl rejects paths without an .xlsx
    # extension, and storage keys deliberately have none.
    with open(path, "rb") as fh:
        try:
            with zipfile.ZipFile(fh) as zf:
                meta = _workbook_sheets(zf)
                scans = {name: _scan_sheet_xml(zf, xml) for name, _, xml in meta}
            fh.seek(0)
            workbook = openpyxl.load_workbook(fh, read_only=True, data_only=True, keep_links=False)
        except Exception as exc:  # zipfile/XML/openpyxl raise many types on damage
            raise PermanentError(ErrorCode.CORRUPT_FILE, details={"error": str(exc)}) from exc
        try:
            hidden = {name: is_hidden for name, is_hidden, _ in meta}
            return [
                _sheet_unit(
                    index,
                    _read_worksheet(
                        ws, hidden.get(ws.title, False), scans.get(ws.title, _XmlScan()), settings
                    ),
                    settings,
                )
                for index, ws in enumerate(workbook.worksheets)
            ]
        finally:
            workbook.close()


def _read_worksheet(ws: Any, hidden: bool, scan: _XmlScan, settings: PipelineSettings) -> SheetData:
    sheet = SheetData(ws.title, hidden, [], scan.merges)
    for row in ws.iter_rows(min_row=1, values_only=True):
        if len(sheet.grid) >= settings.max_sheet_rows:
            sheet.warnings.append(_rows_truncated(settings))
            break
        sheet.grid.append([cell_to_str(v, settings.max_unit_text_chars) for v in row])
    if scan.uncached_formulas:
        sheet.warnings.append(
            unit_warning(
                "FORMULA_NOT_CACHED",
                f"{len(scan.uncached_formulas)} formula cells have no cached value; left empty",
                cells=scan.uncached_formulas[:MAX_REPORTED_CELLS],
            )
        )
    return sheet


# ----------------------------------------------------------------------- XLS


def _xls_cell(cell: xlrd.sheet.Cell, datemode: int, max_chars: int) -> str:
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ""
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return cell_to_str(xlrd.xldate.xldate_as_datetime(cell.value, datemode), max_chars)
        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
            return cell_to_str(cell.value, max_chars)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, "#ERROR")
    return cell_to_str(cell.value, max_chars)


def parse_xls(path: Path, settings: PipelineSettings) -> list[ParsedUnit]:
    try:
        # formatting_info=True is required for merged_cells; xlrd never runs macros.
        book = xlrd.open_workbook(str(path), formatting_info=True, on_demand=True)
    except xlrd.biffh.XLRDError as exc:
        code = ErrorCode.PASSWORD_PROTECTED if "encrypt" in str(exc).lower() else ErrorCode.CORRUPT_FILE
        raise PermanentError(code, details={"error": str(exc)}) from exc
    except Exception as exc:
        raise PermanentError(ErrorCode.CORRUPT_FILE, details={"error": str(exc)}) from exc

    units: list[ParsedUnit] = []
    try:
        for index in range(book.nsheets):
            sh = book.sheet_by_index(index)
            merges = [(r0 + 1, c0 + 1, r1, c1) for r0, r1, c0, c1 in sh.merged_cells]
            sheet = SheetData(sh.name, sh.visibility != XLS_VISIBLE, [], merges)
            for r in range(sh.nrows):
                if r >= settings.max_sheet_rows:
                    sheet.warnings.append(_rows_truncated(settings))
                    break
                sheet.grid.append(
                    [_xls_cell(c, book.datemode, settings.max_unit_text_chars) for c in sh.row(r)]
                )
            units.append(_sheet_unit(index, sheet, settings))
            book.unload_sheet(index)
    finally:
        book.release_resources()
    return units
