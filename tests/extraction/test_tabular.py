"""Deterministic spreadsheet extraction (through the real Step 3 sheet parser)."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl

from app.config import ExtractionSettings, PipelineSettings
from app.extraction.tabular import TableUnit, extract_table, map_headers, normalize_header
from app.pipeline.spreadsheet import parse_xlsx

SETTINGS = ExtractionSettings()

THREE_INVOICES = [
    ["Supplier export March"],
    [],
    ["Inv #", "Date", "Vendor", "Client", "Item", "Qty", "Rate", "Amount", "VAT", "Total", "Currency"],
    [
        "00123",
        "05/03/2024",
        "ACME Ltd",
        "Northwind",
        "Gloves",
        "10",
        "12.50",
        "125.00",
        "45.00",
        "270.00",
        "EUR",
    ],
    ["", "", "", "", "Masks", "5", "20.00", "100.00", "", "", ""],
    ["00124", "09/03/2024", "ACME Ltd", "Northwind", "Tape", "2", "4.50", "9.00", "1.80", "10.80", "EUR"],
    [
        "00125",
        "25/03/2024",
        "ACME Ltd",
        "Contoso",
        "Gauze",
        "1",
        "1.234,56",
        "1.234,56",
        "0",
        "1.234,56",
        "EUR",
    ],
]


def sheet_unit(tmp_path: Path, rows: list[list[str]], name: str = "March") -> TableUnit:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = name
    for row in rows:
        ws.append(row)
    path = tmp_path / "book.bin"
    buf = io.BytesIO()
    wb.save(buf)
    path.write_bytes(buf.getvalue())
    unit = parse_xlsx(path, PipelineSettings(storage_root=tmp_path))[0]
    return TableUnit(
        unit_index=0,
        sheet_name=unit.sheet_name,
        structured_data=unit.structured_data or {},
        text=unit.text or "",
    )


def test_header_synonyms() -> None:
    mapping = map_headers(
        ["Inv #", "Vendor", "Client", "Qty", "Rate", "Line Total", "GST", "Grand Total", "CCY"]
    )
    assert mapping == {
        "invoice_number": "Inv #",
        "supplier_name": "Vendor",
        "customer_name": "Client",
        "quantity": "Qty",
        "unit_price": "Rate",
        "amount": "Line Total",
        "tax": "GST",
        "total": "Grand Total",
        "currency": "CCY",
    }
    assert normalize_header("Invoice No._2") == "invoice no"


def test_sheet_with_three_invoices_gives_three_extractions(tmp_path: Path) -> None:
    drafts = extract_table(sheet_unit(tmp_path, THREE_INVOICES), SETTINGS)
    assert drafts is not None and len(drafts) == 3
    first, second, third = drafts
    assert first.column_values()["invoice_number"] == "00123"  # leading zeros kept
    assert first.value("total") == Decimal("270.0000") and first.value("currency") == "EUR"
    assert [li.description for li in first.line_items] == ["Gloves", "Masks"]
    assert first.value("invoice_date") == date(2024, 3, 5)  # day-first proven by "25/03/2024"
    assert third.value("total") == Decimal("1234.5600")  # "1.234,56" in a mixed-format sheet
    meta = first.field_meta()["total"]
    assert meta["source"] == "rule" and meta["grounded"] is True
    assert meta["evidence_text"] == "sheet March row 4 col Total"
    assert all(d.source == "rule" and d.trace is None for d in drafts)
    assert second.overall_confidence == 1.0


def test_conflicting_header_values_are_nulled(tmp_path: Path) -> None:
    rows = [["Invoice No", "Total"], ["A1", "10.00"], ["A1", "12.00"]]
    draft = extract_table(sheet_unit(tmp_path, rows), SETTINGS)[0]  # type: ignore[index]
    assert draft.value("total") is None
    assert draft.field_meta()["total"]["reason"] == "conflicting_rows"


def test_invoice_level_export_has_no_line_items(tmp_path: Path) -> None:
    rows = [
        ["invoice", "supplier", "date", "net", "vat", "gross", "ccy"],
        ["INV-1", "ACME", "2024-03-05", "225.00", "45.00", "270.00", "EUR"],
    ]
    draft = extract_table(sheet_unit(tmp_path, rows), SETTINGS)[0]  # type: ignore[index]
    assert draft.line_items == []
    assert (draft.value("subtotal"), draft.value("tax")) == (Decimal("225.0000"), Decimal("45.0000"))


def test_unknown_headers_fall_back_to_llm(tmp_path: Path) -> None:
    rows = [["Ref", "When", "Who", "How much"], ["x1", "today", "Bob", "12"]]
    assert extract_table(sheet_unit(tmp_path, rows), SETTINGS) is None


def test_missing_total_and_amount_columns_fall_back(tmp_path: Path) -> None:
    rows = [["Invoice No", "Customer"], ["A1", "Bob"]]
    assert extract_table(sheet_unit(tmp_path, rows), SETTINGS) is None
