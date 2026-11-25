"""Table tests for every normalizer (pure, no I/O)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extraction.normalize import (
    find_amount_strings,
    find_date_strings,
    infer_day_first,
    infer_decimal_separator,
    invoice_number_norm,
    normalize_currency,
    parse_amount,
    parse_date,
    supplier_name_norm,
)

D = Decimal


@pytest.mark.parametrize(
    ("raw", "hint", "value", "ambiguous"),
    [
        ("1,234.56", None, D("1234.56"), False),
        ("1.234,56", None, D("1234.56"), False),
        ("1 234,56", None, D("1234.56"), False),
        ("1 234,56", None, D("1234.56"), False),  # narrow no-break space
        ("1'234.50", None, D("1234.50"), False),  # Swiss
        ("(123.45)", None, D("-123.45"), False),
        ("-123.45", None, D("-123.45"), False),
        ("123.45-", None, D("-123.45"), False),
        ("− 45.00", None, D("-45.00"), False),  # unicode minus
        ("€1,234.00", None, D("1234.00"), False),
        ("1.234,00 EUR", None, D("1234.00"), False),
        ("USD 99", None, D("99"), False),
        ("12,5", None, D("12.5"), False),
        ("1.234.567", None, D("1234567"), False),
        ("1,234,567.89", None, D("1234567.89"), False),
        ("1,23,456.00", None, D("123456.00"), False),  # Indian grouping
        ("0.5", None, D("0.5"), False),
        ("270", None, D("270"), False),
        ("1,234", None, D("1234"), True),  # ambiguous, no evidence
        ("1,234", ",", D("1.234"), False),  # document uses decimal comma
        ("1.234", ",", D("1234"), False),
        ("1.234", None, D("1.234"), True),
        ("12.34567", None, D("12.3457"), False),  # quantized to 4 dp
    ],
)
def test_parse_amount(raw: str, hint: str | None, value: Decimal, ambiguous: bool) -> None:
    parsed = parse_amount(raw, hint)
    assert parsed.value == value
    assert parsed.ambiguous is ambiguous
    assert (parsed.note is not None) is ambiguous


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "N/A", "1.2.3,4.5", "--", "99999999999999999"])
def test_parse_amount_refuses_garbage(raw: str | None) -> None:
    assert parse_amount(raw).value is None


@pytest.mark.parametrize(
    ("raws", "expected"),
    [
        (["1.234,56", "99,00"], ","),
        (["1,234.56", "9.99"], "."),
        (["1,234.56", "1.234,56"], None),  # contradictory document
        (["1,234"], None),  # nothing unambiguous
        (["1.234.567"], ","),
        ([None, "", "12,5"], ","),
    ],
)
def test_infer_decimal_separator(raws: list[str | None], expected: str | None) -> None:
    assert infer_decimal_separator(raws) == expected


@pytest.mark.parametrize(
    ("raw", "day_first", "expected"),
    [
        ("2024-03-05", None, date(2024, 3, 5)),
        ("2024/03/05", None, date(2024, 3, 5)),
        ("25/03/2024", None, date(2024, 3, 25)),  # day > 12
        ("03/25/2024", None, date(2024, 3, 25)),  # US order, unambiguous
        ("05/03/2024", True, date(2024, 3, 5)),
        ("05/03/2024", False, date(2024, 5, 3)),
        ("05.03.2024", True, date(2024, 3, 5)),
        ("05-03-24", True, date(2024, 3, 5)),
        ("07/07/2024", None, date(2024, 7, 7)),  # same either way
        ("5 March 2024", None, date(2024, 3, 5)),
        ("5th Mar. 2024", None, date(2024, 3, 5)),
        ("March 5, 2024", None, date(2024, 3, 5)),
        ("Sept 30 2024", None, date(2024, 9, 30)),
        ("05-Mar-2024", None, date(2024, 3, 5)),
        ("29 Feb 2024", None, date(2024, 2, 29)),  # leap year
        ("1-Jan-99", None, date(1999, 1, 1)),
    ],
)
def test_parse_date(raw: str, day_first: bool | None, expected: date) -> None:
    assert parse_date(raw, day_first=day_first).value == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("05/03/2024", "ambiguous"),
        ("31/02/2024", "impossible"),
        ("29 Feb 2023", "impossible"),
        ("2024-13-01", "impossible"),
        ("next Tuesday", "unrecognized"),
        ("32 March 2024", "impossible"),
    ],
)
def test_parse_date_refuses(raw: str, reason: str) -> None:
    parsed = parse_date(raw)
    assert parsed.value is None and parsed.note is not None and reason in parsed.note


@pytest.mark.parametrize(
    ("raws", "expected"),
    [
        (["25/03/2024"], True),
        (["03/25/2024"], False),
        (["5 March 2024", "05/03/2024"], True),
        (["March 5, 2024"], False),
        (["25/03/2024", "03/25/2024"], None),  # contradictory
        (["05/03/2024"], None),
        (["2024-03-05"], None),  # ISO says nothing about other dates
    ],
)
def test_infer_day_first(raws: list[str], expected: bool | None) -> None:
    assert infer_day_first(raws) is expected


@pytest.mark.parametrize(
    ("raw", "text", "amounts", "expected"),
    [
        ("EUR", "", [], "EUR"),
        ("eur", "", [], "EUR"),
        ("€", "", [], "EUR"),
        ("£", "", [], "GBP"),
        ("₹", "", [], "INR"),
        ("৳", "", [], "BDT"),
        ("US$", "", [], "USD"),
        ("C$", "", [], "CAD"),
        ("A$", "", [], "AUD"),
        ("RMB", "", [], "CNY"),
        ("$", "All prices in USD", [], "USD"),
        ("$", "Amounts in CAD unless stated", [], "CAD"),
        ("¥", "Paid in JPY", [], "JPY"),
        ("kr", "Belopp i SEK", [], "SEK"),
        (None, "", ["€ 1.234,00"], "EUR"),
        (None, "", ["1,234.00 GBP"], "GBP"),
        ("Total EUR (incl. VAT)", "", [], "EUR"),
    ],
)
def test_currency_resolves(raw: str | None, text: str, amounts: list[str], expected: str) -> None:
    assert normalize_currency(raw, document_text=text, amount_raws=amounts).value == expected


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("$", ""),  # never default to USD
        ("$", "USD or CAD accepted"),  # conflicting evidence
        ("¥", ""),
        ("kr", "NOK and DKK"),
        (None, ""),
        ("dollars", ""),
    ],
)
def test_currency_refuses_to_guess(raw: str | None, text: str) -> None:
    parsed = normalize_currency(raw, document_text=text)
    assert parsed.value is None and parsed.note


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ACME Supplies Ltd", "acme supplies"),
        ("ACME Supplies Ltd.", "acme supplies"),
        ("Acme Supplies, Limited", "acme supplies"),
        ("ACME SUPPLIES LTD", "acme supplies"),
        ("Müller GmbH", "müller"),
        ("Tata Consultancy Services Pvt Ltd", "tata consultancy services"),
        ("Contoso, Inc.", "contoso"),
        ("Globex LLC", "globex"),
        ("Initech Corp.", "initech"),
        ("  Wayne   Enterprises  ", "wayne enterprises"),
        ("O'Reilly & Sons Co.", "o reilly sons"),
        ("Straße Bau AG", "strasse bau"),
        ("Ltd", "ltd"),  # never empty
        ("ＡＣＭＥ Ltd", "acme"),  # full-width
        ("", None),
        (None, None),
    ],
)
def test_supplier_name_norm(raw: str | None, expected: str | None) -> None:
    assert supplier_name_norm(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INV-2024-001", "INV2024001"),
        ("inv 2024 001", "INV2024001"),
        ("INV/2024/001", "INV2024001"),
        ("00123", "00123"),  # leading zeros kept
        ("#A-77", "A77"),
        ("ＩＮＶ－１", "INV1"),
        ("  x.y.z  ", "XYZ"),
        ("2024_0042", "20240042"),
        ("", None),
        ("---", None),
        (None, None),
        ("Rg-Nr. 4711", "RGNR4711"),
        ("F 2024 / 12", "F202412"),
        ("ab12cd", "AB12CD"),
        ("№ 15", "NO15"),
    ],
)
def test_invoice_number_norm(raw: str | None, expected: str | None) -> None:
    assert invoice_number_norm(raw) == expected


def test_document_scanners_find_evidence() -> None:
    text = "Date 05/03/2024 Due 25/03/2024\nDelivered 5 March 2024\nTotal 1.234,56 EUR, fee 9,99 (ref 12345)"
    assert find_date_strings(text) == ["05/03/2024", "25/03/2024", "5 March 2024"]
    assert find_amount_strings(text) == ["1.234,56", "9,99"]
    assert infer_day_first(find_date_strings(text)) is True
    assert infer_decimal_separator(find_amount_strings(text)) == ","
