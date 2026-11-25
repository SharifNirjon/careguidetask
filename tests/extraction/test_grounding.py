"""Grounding and confidence fusion."""

from __future__ import annotations

import pytest

from app.extraction.confidence import field_confidence, overall_confidence
from app.extraction.grounding import GroundingFailure, ground
from tests.support.extraction import INVOICE_PAGE, field

UNITS = {0: INVOICE_PAGE, 1: "Payment terms: 30 days"}
MIN = 0.85


def test_exact_evidence_is_grounded() -> None:
    g = ground(field(("INV-2024-001", "Invoice Number: INV-2024-001", 0)), UNITS, min_score=MIN)
    assert g.grounded and g.score == 1.0


def test_case_and_whitespace_insensitive() -> None:
    g = ground(field(("inv-2024-001", "invoice   number:\nINV-2024-001", 0)), UNITS, min_score=MIN)
    assert g.grounded


def test_fabricated_value_is_rejected() -> None:
    g = ground(field(("INV-9999", "Invoice Number: INV-9999", 0)), UNITS, min_score=MIN)
    assert not g.grounded
    assert g.failure in (GroundingFailure.EVIDENCE_NOT_IN_UNIT, GroundingFailure.VALUE_NOT_IN_EVIDENCE)


def test_ocr_noisy_but_real_evidence_passes() -> None:
    noisy_unit = {0: INVOICE_PAGE.replace("Invoice Number: INV-2024-001", "lnvoice Nurnber: INV-2O24-001")}
    g = ground(field(("INV-2O24-001", "Invoice Number: INV-2O24-001", 0)), noisy_unit, min_score=MIN)
    assert g.grounded and MIN <= g.score < 1.0


def test_evidence_from_the_wrong_unit_fails() -> None:
    g = ground(field(("INV-2024-001", "Invoice Number: INV-2024-001", 1)), UNITS, min_score=MIN)
    assert not g.grounded and g.failure is GroundingFailure.EVIDENCE_NOT_IN_UNIT


def test_unit_outside_the_segment_fails() -> None:
    g = ground(field(("x", "x", 7)), UNITS, min_score=MIN)
    assert g.failure is GroundingFailure.UNKNOWN_UNIT


def test_value_must_be_inside_its_evidence() -> None:
    g = ground(field(("280.00", "Total Amount Due 270.00 EUR", 0)), UNITS, min_score=MIN)
    assert not g.grounded and g.failure is GroundingFailure.VALUE_NOT_IN_EVIDENCE


def test_missing_evidence_fails() -> None:
    g = ground(field(("270.00", "", 0)), UNITS, min_score=MIN)
    assert g.failure is GroundingFailure.NO_EVIDENCE


@pytest.mark.parametrize(
    ("llm", "grounding", "ocr", "ambiguity", "expected"),
    [
        (0.95, 1.0, None, None, 0.95),
        (0.95, 0.9, None, None, 0.9),
        (0.95, 1.0, 0.7, None, 0.7),  # OCR quality caps confidence
        (0.4, 1.0, 0.99, None, 0.4),
        (0.9, 1.0, None, 0.5, 0.45),  # ambiguous parse halves it
    ],
)
def test_field_confidence_is_the_weakest_signal(
    llm: float, grounding: float, ocr: float | None, ambiguity: float | None, expected: float
) -> None:
    assert field_confidence(llm, grounding, ocr, ambiguity_factor=ambiguity) == expected


def test_overall_confidence_weights_required_fields() -> None:
    weights = {"invoice_number": 0.5, "total": 0.5}
    assert overall_confidence({"invoice_number": 1.0, "total": 0.6}, weights) == 0.8
    assert overall_confidence({"invoice_number": 1.0, "total": None}, weights) == 0.5
    assert overall_confidence({}, {}) == 0.0
