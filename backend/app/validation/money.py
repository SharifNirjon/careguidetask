"""Money arithmetic for validation: Decimal only, currency-aware rounding, one tolerance rule.

Validation may COMPUTE expected values (for messages and suggestions); it
never writes them into extraction fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

# ISO 4217 minor units that differ from the default of 2.
MINOR_UNITS: Final[Mapping[str, int]] = {
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "UGX": 0,
    "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3, "IQD": 3, "LYD": 3,
}  # fmt: skip
DEFAULT_MINOR_UNITS: Final = 2


def minor_units(currency: str | None) -> int:
    return MINOR_UNITS.get((currency or "").upper(), DEFAULT_MINOR_UNITS)


def minor_unit(currency: str | None) -> Decimal:
    """The smallest amount of the currency: 0.01 EUR, 1 JPY, 0.001 KWD."""
    return Decimal(1).scaleb(-minor_units(currency))


def quantize(amount: Decimal, currency: str | None) -> Decimal:
    return amount.quantize(minor_unit(currency), rounding=ROUND_HALF_UP)


def fmt(amount: Decimal, currency: str | None) -> str:
    """'115.00', '1200', '1.500' — as many decimals as the currency has."""
    return f"{quantize(amount, currency):f}"


@dataclass(frozen=True, slots=True)
class AmountCheck:
    expected: Decimal
    actual: Decimal
    difference: Decimal  # absolute, after quantizing both sides
    allowed: Decimal

    @property
    def mismatch(self) -> bool:
        return self.difference > self.allowed


def compare_amounts(
    expected: Decimal,
    actual: Decimal,
    *,
    currency: str | None,
    lines: int,
    rel_tol: Decimal,
) -> AmountCheck:
    """Is `actual` equal to `expected` within tolerance?

    allowed = max(abs_tol, rel_tol):
      abs_tol = minor_unit x (1 + lines)  -- every line can be rounded once, and
                                             the total once more
      rel_tol = rel_tol x the larger amount -- invoicing software that rounds
                                             tax on the total, FX rounding
    A difference exactly AT the allowance is a match; only exceeding it is a
    mismatch.
    """
    exp, act = quantize(expected, currency), quantize(actual, currency)
    abs_tol = minor_unit(currency) * (1 + max(lines, 0))
    rel = rel_tol * max(abs(exp), abs(act))
    return AmountCheck(exp, act, abs(exp - act), max(abs_tol, rel))
