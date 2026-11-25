"""LLM result -> draft: nulling of ungrounded values, normalization, scoring."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.config import ExtractionSettings
from app.extraction.assemble import NOT_AN_INVOICE_NOTE, assemble_llm
from app.extraction.draft import LLMTrace
from app.llm import TokenUsage
from tests.support.extraction import INVOICE_PAGE, llm_result, page

SETTINGS = ExtractionSettings()
TRACE = LLMTrace("m", "invoice_extraction_v1", [], TokenUsage(), 0, 1, 0, 1)


def full_result(**overrides):
    values = {
        "invoice_number": ("INV-2024-001", "Invoice Number: INV-2024-001", 0),
        "invoice_date": ("05/03/2024", "Date: 05/03/2024", 0),
        "supplier_name": ("ACME Supplies Ltd", "ACME Supplies Ltd", 0),
        "customer_name": ("Northwind Clinic", "Bill To: Northwind Clinic", 0),
        "subtotal": ("225.00", "Subtotal 225.00", 0),
        "tax": ("45.00", "VAT 20% 45.00", 0),
        "total": ("270.00", "Total Amount Due 270.00 EUR", 0),
        "currency": ("EUR", "Total Amount Due 270.00 EUR", 0),
        "line_items": [
            {
                "description": ("Nitrile gloves (box)", "Nitrile gloves (box)", 0),
                "quantity": ("10", "Nitrile gloves (box)  10", 0),
                "unit_price": ("12.50", "10  12.50", 0),
                "amount": ("125.00", "12.50  125.00", 0),
            }
        ],
    }
    values.update(overrides)
    return llm_result(**values)


def test_grounded_values_are_normalized() -> None:
    draft = assemble_llm(full_result(), [page(0, INVOICE_PAGE)], SETTINGS, TRACE)
    cols = draft.column_values()
    assert cols["total"] == Decimal("270.0000") and cols["currency"] == "EUR"
    assert cols["invoice_date"] == date(2024, 3, 5)  # "Due: 25/03/2024" on the page proves day-first
    assert cols["invoice_number_norm"] == "INV2024001" and cols["supplier_name_norm"] == "acme supplies"
    assert draft.line_items[0].amount == Decimal("125.0000")
    meta = draft.field_meta()["total"]
    assert meta["grounded"] is True and meta["source"] == "llm" and meta["evidence_text"]
    assert draft.overall_confidence > 0.9


def test_fabricated_invoice_number_is_nulled_and_recorded() -> None:
    result = full_result(invoice_number=("INV-7777", "Invoice Number: INV-7777", 0))
    draft = assemble_llm(result, [page(0, INVOICE_PAGE)], SETTINGS, TRACE)
    meta = draft.field_meta()["invoice_number"]
    assert draft.value("invoice_number") is None
    assert meta["rejected_value"] == "INV-7777" and meta["reason"] == "ungrounded"
    assert meta["grounded"] is False and meta["confidence"] == 0.0
    assert draft.column_values()["invoice_number_norm"] is None


def test_ambiguous_date_without_evidence_is_null_with_note() -> None:
    text = INVOICE_PAGE.replace("   Due: 25/03/2024", "")
    draft = assemble_llm(full_result(), [page(0, text)], SETTINGS, TRACE)
    assert draft.value("invoice_date") is None
    assert draft.field_meta()["invoice_date"]["reason"] == "unparseable"
    assert any("ambiguous day/month" in u for u in draft.uncertainties)


def test_ocr_confidence_caps_field_confidence() -> None:
    draft = assemble_llm(full_result(), [page(0, INVOICE_PAGE, ocr=0.62)], SETTINGS, TRACE)
    assert draft.fields["total"].confidence == 0.62


def test_null_fields_stay_null_and_cost_overall_confidence() -> None:
    draft = assemble_llm(full_result(total=None, currency=None), [page(0, INVOICE_PAGE)], SETTINGS, TRACE)
    assert draft.value("total") is None and draft.field_meta()["total"]["grounded"] is None
    assert draft.overall_confidence < 0.7


def test_currency_from_amount_symbol_when_no_currency_field() -> None:
    text = INVOICE_PAGE.replace("Total Amount Due 270.00 EUR", "Total Amount Due €270.00")
    result = full_result(total=("€270.00", "Total Amount Due €270.00", 0), currency=None)
    draft = assemble_llm(result, [page(0, text)], SETTINGS, TRACE)
    meta = draft.field_meta()["currency"]
    assert draft.value("currency") == "EUR" and meta["derived_from"] == "amount_symbol"


def test_wrong_total_is_kept_exactly_as_printed() -> None:
    text = INVOICE_PAGE.replace("Total Amount Due 270.00 EUR", "Total Amount Due 280.00 EUR")
    result = full_result(total=("280.00", "Total Amount Due 280.00 EUR", 0))
    draft = assemble_llm(result, [page(0, text)], SETTINGS, TRACE)
    assert draft.value("total") == Decimal("280.0000")  # no "correction" to 270


def test_not_an_invoice_is_recorded() -> None:
    draft = assemble_llm(llm_result(document_type="other"), [page(0, "Meeting notes")], SETTINGS, TRACE)
    assert draft.document_type == "other" and NOT_AN_INVOICE_NOTE in draft.uncertainties
    assert all(f.value is None for f in draft.fields.values())
