"""Duplicate vs cross-document classification (pure) and its rule."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.models.enums import ReconciliationStatus, RuleCode, Severity
from app.validation.crossdoc import (
    KeyComparison,
    classify,
    compare_key_fields,
    trigram_similarity,
    within_days,
)
from app.validation.rules import run_rules
from app.validation.types import MatchKind, SourceKind
from tests.validation.builders import D, candidate, context, document, snapshot

DOC, SHEET = SourceKind.DOCUMENT, SourceKind.SPREADSHEET
EQUAL = KeyComparison((), ())
DIFFERENT = compare_key_fields(snapshot(), snapshot(total=D("280.00")), rel_tol=D("0.005"))


@pytest.mark.parametrize(
    ("kind", "this", "other", "comparison", "later", "expected"),
    [
        # the four rows of the table
        (MatchKind.EXACT, DOC, DOC, EQUAL, True, RuleCode.DUPLICATE_INVOICE),
        (MatchKind.EXACT, DOC, DOC, EQUAL, False, None),  # the EARLIER copy is not the duplicate
        (MatchKind.EXACT, DOC, DOC, DIFFERENT, True, RuleCode.INVOICE_NUMBER_CONFLICT),
        (MatchKind.EXACT, DOC, DOC, DIFFERENT, False, RuleCode.INVOICE_NUMBER_CONFLICT),  # both sides
        (MatchKind.EXACT, DOC, SHEET, EQUAL, True, RuleCode.CROSS_DOC_VERIFIED),
        (MatchKind.EXACT, SHEET, DOC, EQUAL, False, RuleCode.CROSS_DOC_VERIFIED),
        (MatchKind.EXACT, DOC, SHEET, DIFFERENT, False, RuleCode.CROSS_DOC_MISMATCH),
        (MatchKind.EXACT, SHEET, DOC, DIFFERENT, True, RuleCode.CROSS_DOC_MISMATCH),
        # fuzzy: only POSSIBLE_DUPLICATE, only same kind, only on the later one
        (MatchKind.FUZZY, DOC, DOC, EQUAL, True, RuleCode.POSSIBLE_DUPLICATE),
        (MatchKind.FUZZY, DOC, DOC, DIFFERENT, True, RuleCode.POSSIBLE_DUPLICATE),
        (MatchKind.FUZZY, DOC, DOC, EQUAL, False, None),
        (MatchKind.FUZZY, DOC, SHEET, EQUAL, True, None),
    ],
)
def test_classification_table(
    kind: MatchKind,
    this: SourceKind,
    other: SourceKind,
    comparison: KeyComparison,
    later: bool,
    expected: RuleCode | None,
) -> None:
    assert classify(kind, this, other, comparison, this_is_later=later) is expected


def test_key_field_differences_are_listed() -> None:
    other = snapshot(
        total=D("275.00"),
        invoice_date=snapshot().invoice_date.replace(day=6),
        customer_name="Contoso Pharmacy",
    )
    comparison = compare_key_fields(snapshot(), other, rel_tol=D("0.005"))
    assert [d.to_dict() for d in comparison.differences] == [
        {"field": "total", "left": "270.00", "right": "275.00"},
        {"field": "invoice_date", "left": "2024-03-05", "right": "2024-03-06"},
        {"field": "customer_name", "left": "northwind clinic", "right": "contoso pharmacy"},
    ]
    assert comparison.status is ReconciliationStatus.MISMATCHED


def test_one_sided_fields_are_not_differences() -> None:
    """A ledger export without tax or line items must still verify against the PDF."""
    ledger = snapshot(tax=None, subtotal=None, customer_name=None, line_items=())
    comparison = compare_key_fields(snapshot(), ledger, rel_tol=D("0.005"))
    assert comparison.equal
    assert set(comparison.not_compared) == {"subtotal", "tax", "customer_name", "line_count", "line_sum"}


def test_customer_names_are_compared_normalized() -> None:
    a, b = snapshot(customer_name="Northwind Clinic Ltd."), snapshot(customer_name="NORTHWIND CLINIC")
    assert compare_key_fields(a, b, rel_tol=D("0.005")).equal


def test_line_count_and_sum_only_when_both_have_lines() -> None:
    fewer = replace(snapshot(), line_items=snapshot().line_items[:1])
    diffs = {d.field for d in compare_key_fields(snapshot(), fewer, rel_tol=D("0.005")).differences}
    assert diffs == {"line_count", "line_sum"}


def test_within_days() -> None:
    a = snapshot()
    assert within_days(a, replace(a, invoice_date=a.invoice_date.replace(day=8)), 3)
    assert not within_days(a, replace(a, invoice_date=a.invoice_date.replace(day=9)), 3)
    assert not within_days(a, replace(a, invoice_date=None), 3)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    # expected values are pg_trgm's own similarity() results (see the DB test for the live check)
    [
        ("acme", "acme", 1.0),
        ("acme supplies", "acme suplies", 0.8),
        ("acme", "globex", 0.0),
        ("Müller Bau", "muller bau", 0.5714),
        ("ACME Ltd.", "acme", 0.5556),
        ("", "acme", 0.0),
        (None, "acme", 0.0),
    ],
)
def test_trigram_similarity(a: str | None, b: str | None, expected: float) -> None:
    assert trigram_similarity(a, b) == pytest.approx(expected, abs=0.01)


# --------------------------------------------------------------- the rules


def test_later_copy_gets_duplicate_invoice_error() -> None:
    earlier, later = document(created=0), document(created=1)
    found = run_rules(context(snapshot(), doc=later, candidates=(candidate(snapshot(), earlier),)))
    duplicate = [i for i in found if i.rule_code is RuleCode.DUPLICATE_INVOICE]
    assert duplicate[0].severity is Severity.ERROR and duplicate[0].related_extraction_id is not None
    assert "'invoice.pdf'" in duplicate[0].message
    assert run_rules(context(snapshot(), doc=earlier, candidates=(candidate(snapshot(), later),))) == []


def test_identical_file_is_also_reported() -> None:
    earlier, later = document(created=0, sha="b" * 64), document(created=1, sha="b" * 64, name="copy.pdf")
    found = {
        i.rule_code
        for i in run_rules(context(snapshot(), doc=later, candidates=(candidate(snapshot(), earlier),)))
    }
    assert found == {RuleCode.DUPLICATE_FILE, RuleCode.DUPLICATE_INVOICE}


def test_cross_doc_mismatch_lists_diffs_in_message_and_expected() -> None:
    ledger = document(SourceKind.SPREADSHEET, name="ledger.xlsx")
    found = run_rules(context(snapshot(), candidates=(candidate(snapshot(total=D("275.00")), ledger),)))
    mismatch = next(i for i in found if i.rule_code is RuleCode.CROSS_DOC_MISMATCH)
    assert (
        "total: 270.00 vs 275.00" in mismatch.message and mismatch.expected_value == "total: 270.00 vs 275.00"
    )


def test_fuzzy_match_is_only_a_warning() -> None:
    other = snapshot(invoice_number="X-77", invoice_number_norm="X77")
    found = run_rules(
        context(
            snapshot(),
            doc=document(created=1),
            candidates=(candidate(other, document(created=0), MatchKind.FUZZY, 0.86),),
        )
    )
    assert [(i.rule_code, i.severity) for i in found] == [(RuleCode.POSSIBLE_DUPLICATE, Severity.WARNING)]
