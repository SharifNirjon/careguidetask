"""Every rule: pass, fail and boundary cases (pure, no DB)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import ValidationSettings
from app.models.enums import RuleCode, Severity
from app.validation.rules import run_rules
from app.validation.types import Issue
from tests.validation.builders import TODAY, D, context, line, snapshot, with_meta

ARITHMETIC = {
    RuleCode.LINE_AMOUNT_MISMATCH,
    RuleCode.LINE_SUM_MISMATCH,
    RuleCode.TOTAL_MISMATCH,
    RuleCode.TAX_INCLUSIVE_LINES,
}


def issues_for(**overrides: object) -> list[Issue]:
    return run_rules(context(snapshot(**overrides)))


def codes(issues: list[Issue]) -> set[RuleCode]:
    return {i.rule_code for i in issues}


def only(issues: list[Issue], code: RuleCode) -> list[Issue]:
    return [i for i in issues if i.rule_code is code]


def test_clean_invoice_has_no_issues() -> None:
    assert issues_for() == []


# ---------------------------------------------------------------- completeness


@pytest.mark.parametrize(
    ("document_type", "missing", "severity"),
    [
        ("invoice", "invoice_number", Severity.ERROR),
        ("invoice", "total", Severity.ERROR),
        ("invoice", "currency", Severity.ERROR),
        ("invoice", "customer_name", Severity.WARNING),
        ("invoice", "tax", Severity.WARNING),
        ("receipt", "invoice_number", Severity.WARNING),  # receipts often have no number
        ("receipt", "customer_name", Severity.WARNING),
        ("receipt", "total", Severity.ERROR),
        ("credit_note", "invoice_date", Severity.ERROR),
    ],
)
def test_missing_field_severity_by_document_type(
    document_type: str, missing: str, severity: Severity
) -> None:
    found = only(issues_for(document_type=document_type, **{missing: None}), RuleCode.MISSING_FIELD)
    assert [(i.field_path, i.severity) for i in found] == [(missing, severity)]


def test_receipt_without_customer_or_number_has_no_errors() -> None:
    found = issues_for(
        document_type="receipt", invoice_number=None, invoice_number_norm=None, customer_name=None
    )
    assert all(i.severity is not Severity.ERROR for i in found)


def test_rejected_value_is_not_also_reported_missing() -> None:
    extraction = with_meta(
        snapshot(invoice_number=None, invoice_number_norm=None),
        "invoice_number",
        rejected_value="INV-9999",
        reason="ungrounded",
        grounded=False,
        confidence=0.0,
    )
    found = run_rules(context(extraction))
    assert RuleCode.MISSING_FIELD not in codes(found)
    ungrounded = only(found, RuleCode.UNGROUNDED_VALUE)
    assert ungrounded[0].severity is Severity.ERROR and "'INV-9999'" in ungrounded[0].message


@pytest.mark.parametrize(
    ("name", "severity"), [("total", Severity.ERROR), ("customer_name", Severity.WARNING)]
)
def test_ungrounded_severity(name: str, severity: Severity) -> None:
    extraction = with_meta(snapshot(**{name: None}), name, rejected_value="X", reason="ungrounded")
    assert only(run_rules(context(extraction)), RuleCode.UNGROUNDED_VALUE)[0].severity is severity


def test_ungrounded_line_item_value() -> None:
    lines = (
        replace(
            line(0, "10", "12.50", "125.00"),
            field_meta={"amount": {"rejected_value": "999", "reason": "ungrounded"}},
        ),
    )
    found = only(
        issues_for(line_items=lines, subtotal=D("125.00"), tax=D("25.00"), total=D("150.00")),
        RuleCode.UNGROUNDED_VALUE,
    )
    assert found[0].field_path == "line_items[0].amount"


@pytest.mark.parametrize(("confidence", "flagged"), [(0.59, True), (0.6, False), (0.95, False)])
def test_low_field_confidence_boundary(confidence: float, flagged: bool) -> None:
    extraction = with_meta(snapshot(), "total", confidence=confidence)
    assert (RuleCode.LOW_CONFIDENCE in codes(run_rules(context(extraction)))) is flagged


@pytest.mark.parametrize(("overall", "flagged"), [(0.69, True), (0.7, False)])
def test_low_overall_confidence_boundary(overall: float, flagged: bool) -> None:
    found = only(issues_for(overall_confidence=overall), RuleCode.LOW_CONFIDENCE)
    assert bool(found) is flagged and all(i.field_path == "overall_confidence" for i in found)


def test_not_an_invoice_skips_required_fields() -> None:
    found = issues_for(document_type="other", invoice_number=None, total=None, currency=None)
    assert codes(found) == {RuleCode.NOT_AN_INVOICE}


# ------------------------------------------------------------- numeric validity


def test_invalid_amount() -> None:
    extraction = with_meta(snapshot(total=None), "total", rejected_value="2,70,0.0", reason="unparseable")
    found = only(run_rules(context(extraction)), RuleCode.INVALID_AMOUNT)
    assert found[0].severity is Severity.ERROR and found[0].actual_value == "2,70,0.0"


@pytest.mark.parametrize(
    ("document_type", "total", "flagged"),
    [("invoice", "-270.00", True), ("receipt", "-1.00", True), ("credit_note", "-270.00", False)],
)
def test_negative_amount_only_on_non_credit_notes(document_type: str, total: str, flagged: bool) -> None:
    found = issues_for(
        document_type=document_type, subtotal=D(total) * 225 / 270, tax=None, total=D(total), line_items=()
    )
    assert (RuleCode.NEGATIVE_AMOUNT in codes(found)) is flagged


@pytest.mark.parametrize(
    ("subtotal", "tax", "total", "flagged"),
    [
        ("-40.00", "-8.00", "-48.00", False),
        ("-40.00", "8.00", "-32.00", True),
        ("-40.00", "0", "-40.00", False),
    ],
)
def test_credit_note_signs(subtotal: str, tax: str, total: str, flagged: bool) -> None:
    found = issues_for(
        document_type="credit_note", subtotal=D(subtotal), tax=D(tax), total=D(total), line_items=()
    )
    assert (RuleCode.SIGN_INCONSISTENT in codes(found)) is flagged
    assert RuleCode.NEGATIVE_AMOUNT not in codes(found)


@pytest.mark.parametrize(
    ("subtotal", "tax", "flagged"),
    [
        ("100.00", "50.00", False),
        ("100.00", "50.01", True),
        ("100.00", "-1.00", True),
        ("100.00", "0", False),
    ],
)
def test_implausible_tax_rate_boundary(subtotal: str, tax: str, flagged: bool) -> None:
    found = issues_for(subtotal=D(subtotal), tax=D(tax), total=D(subtotal) + D(tax), line_items=())
    assert (RuleCode.IMPLAUSIBLE_TAX_RATE in codes(found)) is flagged


def test_zero_total() -> None:
    found = issues_for(subtotal=D(0), tax=D(0), total=D(0), line_items=())
    assert codes(found) == {RuleCode.ZERO_TOTAL}


# ------------------------------------------------------ arithmetic consistency


def test_total_mismatch_message_shows_the_math() -> None:
    found = only(
        issues_for(subtotal=D("100.00"), tax=D("15.00"), total=D("120.00"), line_items=()),
        RuleCode.TOTAL_MISMATCH,
    )
    assert found[0].message == "Subtotal 100.00 + tax 15.00 = 115.00, but total is 120.00 (difference 5.00)."
    assert (found[0].expected_value, found[0].actual_value, found[0].field_path) == (
        "115.00",
        "120.00",
        "total",
    )


@pytest.mark.parametrize(
    ("total", "flagged"),
    # allowed = max(1 cent, 0.5% of the larger): at 115.57 it's 0.5779 >= 0.57; at 115.58 0.5779 < 0.58
    [("115.01", False), ("115.57", False), ("115.58", True), ("120.00", True)],
)
def test_total_mismatch_tolerance_boundary(total: str, flagged: bool) -> None:
    found = issues_for(subtotal=D("100.00"), tax=D("15.00"), total=D(total), line_items=())
    assert (RuleCode.TOTAL_MISMATCH in codes(found)) is flagged


def test_line_amount_mismatch_per_line() -> None:
    lines = (line(0, "10", "12.50", "125.00"), line(1, "5", "20.00", "110.00"))
    found = only(
        issues_for(line_items=lines, subtotal=D("235.00"), tax=D("47.00"), total=D("282.00")),
        RuleCode.LINE_AMOUNT_MISMATCH,
    )
    assert [i.field_path for i in found] == ["line_items[1].amount"]
    assert found[0].expected_value == "100.00" and "5 x 20.00 = 100.00" in found[0].message


def test_line_sum_mismatch() -> None:
    lines = (line(0, "10", "12.50", "125.00"), line(1, "5", "20.00", "100.00"))
    found = only(
        issues_for(line_items=lines, subtotal=D("300.00"), tax=D("60.00"), total=D("360.00")),
        RuleCode.LINE_SUM_MISMATCH,
    )
    assert found[0].severity is Severity.ERROR and found[0].expected_value == "225.00"


def test_tax_inclusive_lines_are_info_not_error() -> None:
    lines = (line(0, "1", "150.00", "150.00"), line(1, "1", "120.00", "120.00"))  # gross line prices
    found = issues_for(line_items=lines)  # subtotal 225 + tax 45 = total 270 = Σ lines
    assert RuleCode.LINE_SUM_MISMATCH not in codes(found)
    assert [(i.rule_code, i.severity) for i in found] == [(RuleCode.TAX_INCLUSIVE_LINES, Severity.INFO)]


def test_missing_subtotal_compares_lines_with_total_minus_tax() -> None:
    assert RuleCode.LINE_SUM_MISMATCH not in codes(issues_for(subtotal=None))
    found = only(issues_for(subtotal=None, total=D("300.00")), RuleCode.LINE_SUM_MISMATCH)
    assert "total 300.00 - tax 45.00 = 255.00 (no subtotal printed)" in found[0].message


def test_no_line_items_skips_line_checks() -> None:
    assert not codes(issues_for(line_items=())) & ARITHMETIC


def test_incomplete_line_amounts_skip_the_sum_check() -> None:
    lines = (line(0, "10", "12.50", "125.00"), line(1, None, None, None))
    assert RuleCode.LINE_SUM_MISMATCH not in codes(issues_for(line_items=lines))


def test_twenty_lines_of_rounding_noise_pass() -> None:
    lines = tuple(line(i, "3", "0.333", "1.00") for i in range(20))  # each 0.999 printed as 1.00
    found = issues_for(line_items=lines, subtotal=D("19.98"), tax=D("4.00"), total=D("23.98"))
    assert not codes(found) & ARITHMETIC


@pytest.mark.parametrize(
    ("currency", "subtotal", "tax", "total", "flagged"),
    [
        ("JPY", "10000", "1000", "11000", False),
        ("JPY", "10000", "1000", "11001", False),
        ("JPY", "100", "10", "112", True),
        ("KWD", "1.000", "0.050", "1.051", False),
        ("KWD", "1.000", "0.050", "1.055", False),
        ("KWD", "1.000", "0.050", "1.060", True),
    ],
)
def test_zero_and_three_decimal_currencies(
    currency: str, subtotal: str, tax: str, total: str, flagged: bool
) -> None:
    found = issues_for(currency=currency, subtotal=D(subtotal), tax=D(tax), total=D(total), line_items=())
    assert (RuleCode.TOTAL_MISMATCH in codes(found)) is flagged


# ---------------------------------------------------------------- dates/formats


def test_invalid_date() -> None:
    extraction = with_meta(
        snapshot(invoice_date=None),
        "invoice_date",
        rejected_value="31/02/2024",
        reason="unparseable",
        note="impossible date",
    )
    found = only(run_rules(context(extraction)), RuleCode.INVALID_DATE)
    assert found[0].severity is Severity.ERROR and "impossible date" in found[0].message


@pytest.mark.parametrize(("days_ahead", "flagged"), [(0, False), (7, False), (8, True)])
def test_future_date_grace(days_ahead: int, flagged: bool) -> None:
    found = issues_for(invoice_date=TODAY + timedelta(days=days_ahead))
    assert (RuleCode.FUTURE_DATE in codes(found)) is flagged


@pytest.mark.parametrize(("issued", "flagged"), [(date(2019, 4, 1), False), (date(2019, 3, 31), True)])
def test_date_too_old_boundary(issued: date, flagged: bool) -> None:
    assert (RuleCode.DATE_TOO_OLD in codes(issues_for(invoice_date=issued))) is flagged


def test_unknown_currency() -> None:
    assert only(issues_for(currency="XYZ"), RuleCode.UNKNOWN_CURRENCY)[0].severity is Severity.ERROR
    unresolved = with_meta(
        snapshot(currency=None),
        "currency",
        rejected_value="$",
        reason="unparseable",
        note="currency symbol '$' is ambiguous",
    )
    found = run_rules(context(unresolved))
    assert RuleCode.UNKNOWN_CURRENCY in codes(found) and RuleCode.MISSING_FIELD not in codes(found)


@pytest.mark.parametrize(
    ("number", "norm", "flagged"),
    [
        ("INV-1", "INV1", False),
        ("7", "7", True),
        ("--", None, True),
        ("A" * 41, "A" * 41, True),
        ("A" * 40, "A" * 40, False),
    ],
)
def test_invoice_number_format(number: str, norm: str | None, flagged: bool) -> None:
    found = issues_for(invoice_number=number, invoice_number_norm=norm)
    assert (RuleCode.INVOICE_NUMBER_FORMAT in codes(found)) is flagged


# ------------------------------------------------------------------- registry


def test_rules_can_be_disabled_and_severities_overridden() -> None:
    extraction = snapshot(subtotal=D("100.00"), tax=D("15.00"), total=D("120.00"), line_items=())
    disabled = ValidationSettings(disabled_rules=["TOTAL_MISMATCH"])
    assert RuleCode.TOTAL_MISMATCH not in codes(run_rules(context(extraction, settings=disabled)))
    softened = ValidationSettings(severity_overrides={"TOTAL_MISMATCH": "warning"})
    assert (
        only(run_rules(context(extraction, settings=softened)), RuleCode.TOTAL_MISMATCH)[0].severity
        is Severity.WARNING
    )


# ------------------------------------------------------------- property-based

cents = st.integers(min_value=1, max_value=10_000_000).map(lambda c: Decimal(c).scaleb(-2))
rates = st.sampled_from([Decimal("0"), Decimal("0.05"), Decimal("0.07"), Decimal("0.19"), Decimal("0.2")])


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.integers(1, 50), cents), min_size=0, max_size=25), rates)
def test_consistent_invoices_have_no_arithmetic_issues(
    rows: list[tuple[int, Decimal]], rate: Decimal
) -> None:
    lines = tuple(line(i, str(q), str(p), str(q * p)) for i, (q, p) in enumerate(rows))
    subtotal = sum((q * p for q, p in rows), Decimal(0)) or Decimal("10.00")
    tax = (subtotal * rate).quantize(Decimal("0.01"))
    found = issues_for(line_items=lines, subtotal=subtotal, tax=tax, total=subtotal + tax)
    assert not codes(found) & ARITHMETIC


@settings(max_examples=200, deadline=None)
@given(cents, rates, st.integers(min_value=1, max_value=1_000_000))
def test_total_beyond_tolerance_is_always_flagged(subtotal: Decimal, rate: Decimal, extra_cents: int) -> None:
    tax = (subtotal * rate).quantize(Decimal("0.01"))
    honest = subtotal + tax
    allowed = max(Decimal("0.01"), Decimal("0.005") * (honest * 2))
    total = honest + allowed + Decimal(extra_cents).scaleb(-2) + allowed
    found = issues_for(line_items=(), subtotal=subtotal, tax=tax, total=total)
    assert RuleCode.TOTAL_MISMATCH in codes(found)
