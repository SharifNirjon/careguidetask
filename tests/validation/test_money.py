"""The single tolerance rule, and currency rounding."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.validation.money import compare_amounts, fmt, minor_unit, minor_units, quantize

D = Decimal
REL = D("0.005")


@pytest.mark.parametrize(
    ("currency", "units"),
    [("EUR", 2), ("usd", 2), (None, 2), ("JPY", 0), ("KRW", 0), ("BHD", 3), ("KWD", 3), ("OMR", 3)],
)
def test_minor_units(currency: str | None, units: int) -> None:
    assert minor_units(currency) == units


def test_quantize_and_format() -> None:
    assert quantize(D("1.005"), "EUR") == D("1.01")  # half-up, not banker's
    assert fmt(D("1200.4"), "JPY") == "1200"
    assert fmt(D("1.5"), "KWD") == "1.500"
    assert minor_unit("KWD") == D("0.001")


NO_REL = D(0)


@pytest.mark.parametrize(
    ("expected", "actual", "currency", "lines", "rel", "mismatch"),
    [
        # absolute tolerance alone: 1 minor unit x (1 + lines)
        ("100.00", "100.00", "EUR", 0, NO_REL, False),
        ("10.00", "10.01", "EUR", 0, NO_REL, False),  # exactly at 1 cent: match
        ("10.00", "10.02", "EUR", 0, NO_REL, True),
        ("10.00", "10.03", "EUR", 2, NO_REL, False),  # exactly at 3 cents with 2 lines
        ("10.00", "10.04", "EUR", 2, NO_REL, True),
        # relative tolerance: 0.5 % of the larger amount
        ("995.00", "1000.00", "EUR", 0, REL, False),  # exactly at 5.00: match
        ("994.99", "1000.00", "EUR", 0, REL, True),
        ("10000.00", "10050.00", "EUR", 0, REL, False),
        ("10000.00", "10051.00", "EUR", 0, REL, True),  # 51.00 > 50.255
        ("10.00", "10.04", "EUR", 0, REL, False),  # 0.5 % of 10.04 = 0.0502 beats 1 cent
        # zero-decimal and three-decimal currencies
        ("1200", "1201", "JPY", 0, NO_REL, False),  # 1 yen
        ("1200", "1202", "JPY", 0, NO_REL, True),
        ("1.000", "1.001", "KWD", 0, NO_REL, False),  # 1 fils
        ("1.000", "1.002", "KWD", 0, NO_REL, True),
        # sign
        ("-48.00", "-48.00", "EUR", 0, REL, False),
        ("-48.00", "48.00", "EUR", 0, REL, True),
    ],
)
def test_compare_amounts(
    expected: str, actual: str, currency: str, lines: int, rel: Decimal, mismatch: bool
) -> None:
    check = compare_amounts(D(expected), D(actual), currency=currency, lines=lines, rel_tol=rel)
    assert check.mismatch is mismatch


def test_twenty_lines_with_per_line_rounding_is_not_a_mismatch() -> None:
    """20 lines of 3 x 0.333: each printed line rounds 0.999 -> 1.00, so the
    printed sum is 20.00 while the exact sum is 19.98. 2 cents of per-line
    rounding sit well inside the 21-cent allowance for 20 lines."""
    printed_lines = [quantize(D("3") * D("0.333"), "EUR") for _ in range(20)]
    exact_sum = D("3") * D("0.333") * 20
    check = compare_amounts(sum(printed_lines, D(0)), exact_sum, currency="EUR", lines=20, rel_tol=NO_REL)
    assert check.difference == D("0.02") and not check.mismatch
    assert check.allowed == D("0.21")


def test_difference_exactly_at_tolerance_is_a_match() -> None:
    absolute = compare_amounts(D("10.00"), D("10.01"), currency="EUR", lines=0, rel_tol=NO_REL)
    relative = compare_amounts(D("995.00"), D("1000.00"), currency="EUR", lines=0, rel_tol=REL)
    for check in (absolute, relative):
        assert check.difference == check.allowed and not check.mismatch


money = st.decimals(min_value=D("0.01"), max_value=D("1000000"), places=2, allow_nan=False)


@given(money, st.integers(min_value=0, max_value=50))
def test_equal_amounts_never_mismatch(amount: Decimal, lines: int) -> None:
    assert not compare_amounts(amount, amount, currency="EUR", lines=lines, rel_tol=REL).mismatch


@given(money, st.integers(min_value=0, max_value=50))
def test_beyond_both_tolerances_always_mismatches(amount: Decimal, lines: int) -> None:
    allowed = max(D("0.01") * (1 + lines), REL * (amount * 2))
    pushed = amount + allowed * 2 + D("0.01")
    assert compare_amounts(amount, pushed, currency="EUR", lines=lines, rel_tol=REL).mismatch
