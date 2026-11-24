"""Spreadsheet (XLSX/XLS) and CSV parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PipelineSettings
from app.models.enums import ExtractionMethod, UnitType
from app.pipeline import csv_parser, spreadsheet
from tests.fixtures import factory


@pytest.fixture
def xlsx_units(fixture_files: dict[str, Path], settings: PipelineSettings):
    return spreadsheet.parse_xlsx(fixture_files["invoices.xlsx"], settings)


def _block(unit, header_row: int) -> dict:
    return next(b for b in unit.structured_data["table_blocks"] if b["header_row"] == header_row)


def _rows(unit, block_index: int) -> list[dict]:
    return [r for r in unit.structured_data["rows"] if r["_block"] == block_index]


def test_every_sheet_is_a_unit_including_hidden(xlsx_units) -> None:
    assert [(u.unit_type, u.unit_index, u.sheet_name) for u in xlsx_units] == [
        (UnitType.SHEET, 0, "Invoices"),
        (UnitType.SHEET, 1, "Summary"),
        (UnitType.SHEET, 2, "Lookup"),
    ]
    assert [u.structured_data["hidden"] for u in xlsx_units] == [False, False, True]
    assert all(u.extraction_method is ExtractionMethod.SPREADSHEET for u in xlsx_units)


def test_header_on_row_4_and_raw_strings(xlsx_units) -> None:
    sheet = xlsx_units[0]
    block = _block(sheet, 4)
    assert block["headers"] == ["Invoice No", "Date", "Customer", "Amount", "Currency"]
    rows = _rows(sheet, block["index"])
    assert [r["Invoice No"] for r in rows] == ["00123", "00124", "00125"]  # leading zeros kept
    assert rows[0]["Amount"] == "1.234,56"  # not coerced
    assert rows[2]["Amount"] == "450.5"  # numeric cell -> its raw string
    assert sheet.structured_data["headers"] == block["headers"]  # primary block


def test_merged_cells_are_forward_filled(xlsx_units) -> None:
    rows = _rows(xlsx_units[0], _block(xlsx_units[0], 4)["index"])
    assert [r["Currency"] for r in rows] == ["EUR", "EUR", "EUR"]


def test_multiple_tables_detected(xlsx_units) -> None:
    blocks = xlsx_units[0].structured_data["table_blocks"]
    assert len(blocks) == 3  # title block, invoice table, notes table
    assert _block(xlsx_units[0], 10)["headers"] == ["Notes", "Status"]


def test_uncached_formula_is_a_warning_not_a_guess(xlsx_units) -> None:
    warning = next(w for w in xlsx_units[0].warnings if w["code"] == "FORMULA_NOT_CACHED")
    assert warning["details"]["cells"] == ["F5"]
    assert "F5" not in xlsx_units[0].text  # nothing invented


def test_text_rendering_is_pipe_delimited(xlsx_units) -> None:
    assert "00123 | 2024-03-05 | Northwind Clinic | 1.234,56 | EUR" in xlsx_units[0].text
    assert xlsx_units[2].text.startswith("Sheet: Lookup (hidden)")


def test_sheet_row_limit(tmp_path: Path, fixture_files: dict[str, Path]) -> None:
    settings = PipelineSettings(storage_root=tmp_path, max_sheet_rows=3)
    units = spreadsheet.parse_xlsx(fixture_files["invoices.xlsx"], settings)
    assert any(w["code"] == "ROWS_TRUNCATED" for w in units[0].warnings)


def test_xls_fixture(fixture_files: dict[str, Path], settings: PipelineSettings) -> None:
    units = spreadsheet.parse_xls(fixture_files["invoice.xls"], settings)
    assert [(u.sheet_name, u.structured_data["hidden"]) for u in units] == [
        ("Invoices", False),
        ("Rates", True),
    ]
    rows = [r for r in units[0].structured_data["rows"] if "Invoice No" in r]
    assert rows[0] == {
        "Invoice No": "00123",
        "Date": "2024-03-05",
        "Customer": "Northwind Clinic",
        "Amount": "1.234,56",
        "_row": 4,
        "_block": 1,
    }
    assert rows[1]["Amount"] == "99"


# ---------------------------------------------------------------------- CSV


@pytest.mark.parametrize(
    ("name", "delimiter"),
    [("semicolon_cp1252.csv", ";"), ("utf8_bom.csv", ","), ("utf16.csv", "\t")],
)
def test_csv_encodings_and_delimiters(
    fixture_files: dict[str, Path], settings: PipelineSettings, name: str, delimiter: str
) -> None:
    unit = csv_parser.parse_csv(fixture_files[name], settings)
    data = unit.structured_data
    assert unit.unit_type is UnitType.CSV and data["delimiter"] == delimiter
    assert data["headers"] == ["Invoice", "Supplier", "Amount", "Date"]
    first = data["rows"][0]
    assert first["Invoice"] == "00123"  # leading zeros preserved
    assert first["Supplier"] == "Café Müller"
    assert first["Amount"] == "1.234,56"
    assert "﻿" not in data["headers"][0]  # BOM consumed


def test_malformed_rows_are_counted_not_fatal(
    fixture_files: dict[str, Path], settings: PipelineSettings
) -> None:
    unit = csv_parser.parse_csv(fixture_files["malformed.csv"], settings)
    warning = next(w for w in unit.warnings if w["code"] == "MALFORMED_ROWS")
    assert warning["details"]["count"] == 2
    assert unit.structured_data["n_rows"] == 4


def test_nul_bytes_in_csv_are_stripped(tmp_path: Path, settings: PipelineSettings) -> None:
    path = tmp_path / "nul.csv"
    path.write_bytes(b"Invoice,Amount\r\nINV\x00-1,10\r\nINV-2,20\r\n")
    unit = csv_parser.parse_csv(path, settings)
    assert unit.structured_data["rows"][0]["Invoice"] == "INV-1"
    assert "\x00" not in unit.text


def test_csv_shape_matches_sheet_shape(fixture_files: dict[str, Path], settings: PipelineSettings) -> None:
    csv_unit = csv_parser.parse_csv(fixture_files["semicolon_cp1252.csv"], settings)
    sheet = spreadsheet.parse_xlsx(fixture_files["invoices.xlsx"], settings)[0]
    common = {"headers", "rows", "table_blocks", "hidden", "n_rows", "n_cols", "warnings"}
    assert common <= csv_unit.structured_data.keys() and common <= sheet.structured_data.keys()


def test_sniffer_rejects_prose() -> None:
    assert csv_parser.sniff_delimiter("This is just a sentence.\nAnd another one here.") is None
    assert csv_parser.looks_like_csv(factory.csv_bytes())
