"""Evaluation metrics on tiny hand-built cases with known answers."""

from __future__ import annotations

import itertools
import random

import pytest

from eval import metrics as M


def test_field_outcomes_after_normalization() -> None:
    assert M.field_outcome("total", "270.00", "270") == "correct"
    assert M.field_outcome("total", "270.00", "270.01") == "wrong"
    assert M.field_outcome("total", "270.00", None) == "missing"
    assert M.field_outcome("tax", None, "45.00") == "hallucinated"
    assert M.field_outcome("tax", None, None) == "true_null"
    assert M.field_outcome("invoice_number", "EV-2026-0042", "ev 2026/0042") == "correct"
    assert M.field_outcome("currency", "EUR", "eur") == "correct"
    assert M.field_outcome("invoice_date", "2026-03-04", "2026-04-03") == "wrong"
    assert M.field_outcome("supplier_name", "Brightwell Medical Supplies Ltd", "BRIGHTWELL medical supplies, ltd") == "correct"


def test_lenient_names_only_with_the_flag() -> None:
    exp, pred = "Kestrel Lab Services GmbH", "Kestrel Lab Service GmbH"
    assert M.field_outcome("supplier_name", exp, pred) == "wrong"
    assert M.field_outcome("supplier_name", exp, pred, lenient=True) == "correct"
    assert M.field_outcome("total", "1.00", "1.10", lenient=True) == "wrong"  # amounts are never lenient


def test_rates_on_a_known_case() -> None:
    counts = M.Counts()
    for outcome in ["correct"] * 6 + ["wrong"] + ["missing"] * 2 + ["hallucinated"] + ["true_null"] * 3:
        counts.add(outcome)  # type: ignore[arg-type]
    rates = {k: (r.k, r.n) for k, r in counts.rates().items()}
    assert rates == {
        "accuracy": (9, 13),
        "precision": (6, 8),
        "recall": (6, 9),
        "missing_rate": (2, 9),
        "hallucination_rate": (1, 4),
    }


def test_wilson_interval_known_values() -> None:
    low, high = M.wilson(8, 10)
    assert low == pytest.approx(0.4902, abs=1e-4)
    assert high == pytest.approx(0.9433, abs=1e-4)
    assert M.wilson(0, 0) == (0.0, 1.0)
    low, high = M.wilson(0, 20)
    assert low == 0.0 and high == pytest.approx(0.1611, abs=1e-4)
    assert M.Rate(3, 4).as_dict()["small_n"] is True


def test_percentiles_match_linear_interpolation() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert M.percentile(values, 50) == 25.0
    assert M.percentile(values, 90) == pytest.approx(37.0)
    assert M.percentile([5.0], 95) == 5.0
    assert M.percentile([], 50) is None
    dist = M.distribution([1.0, 2.0, 3.0])
    assert (dist["n"], dist["p50"], dist["max"]) == (3, 2.0, 3.0)


def test_hungarian_matches_brute_force() -> None:
    rng = random.Random(3)
    for rows, cols in [(1, 1), (3, 3), (4, 2), (2, 5), (6, 6)]:
        cost = [[rng.randint(0, 20) for _ in range(cols)] for _ in range(rows)]
        pairs = M.hungarian(cost)
        assert len(pairs) == min(rows, cols)
        assert len({r for r, _ in pairs}) == len({c for _, c in pairs}) == len(pairs)
        got = sum(cost[r][c] for r, c in pairs)
        if rows <= cols:
            best = min(sum(cost[r][c] for r, c in enumerate(perm)) for perm in itertools.permutations(range(cols), rows))
        else:
            best = min(sum(cost[r][c] for c, r in enumerate(perm)) for perm in itertools.permutations(range(rows), cols))
        assert got == best


def test_invoices_pair_by_number_then_by_fields() -> None:
    expected = [
        {"invoice_number": "A-1", "total": "10.00", "invoice_date": "2026-01-01"},
        {"invoice_number": None, "total": "99.00", "invoice_date": "2026-02-02", "supplier_name": "X Ltd"},
        {"invoice_number": "C-3", "total": "5.00"},
    ]
    predicted = [
        {"invoice_number": "B-9", "total": "99.00", "invoice_date": "2026-02-02", "supplier_name": "x ltd"},
        {"invoice_number": "a1", "total": "10.00"},
        {"invoice_number": "Z-0", "total": "1.00"},
    ]
    match = M.match_invoices(expected, predicted)
    assert match.pairs == [(0, 1), (1, 0)]
    assert match.missed == [2]
    assert match.spurious == [2]


def test_line_item_precision_recall_f1() -> None:
    expected = [
        {"description": "Nitrile gloves (box of 100)", "quantity": "2", "unit_price": "10.00", "amount": "20.00"},
        {"description": "Courier delivery", "quantity": "1", "unit_price": "7.50", "amount": "7.50"},
        {"description": "Hand sanitiser 5L", "quantity": "3", "unit_price": "20.00", "amount": "60.00"},
    ]
    predicted = [
        {"description": "Courier delivery", "quantity": "1", "unit_price": "7.50", "amount": "7.50"},
        {"description": "Nitrile gloves (box of 100)", "quantity": "2", "unit_price": "10.00", "amount": "21.00"},
        {"description": "Totally unrelated fee", "quantity": "1", "unit_price": "3.00", "amount": "3.00"},
        {"description": "Another invented row", "quantity": "1", "unit_price": "4.00", "amount": "4.00"},
    ]
    score = M.score_lines(expected, predicted).as_dict()
    assert (score["tp"], score["fp"], score["fn"]) == (2, 2, 1)
    assert score["precision"]["value"] == pytest.approx(0.5)
    assert score["recall"]["value"] == pytest.approx(2 / 3)
    assert score["f1"] == pytest.approx(2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))
    assert (score["attribute_accuracy"]["amount"]["k"], score["attribute_accuracy"]["amount"]["n"]) == (1, 2)
    assert M.score_lines([], []).as_dict()["f1"] is None


def test_expected_calibration_error() -> None:
    pairs = [(0.95, True)] * 9 + [(0.95, False)] + [(0.55, True)] * 2 + [(0.55, False)] * 2
    cal = M.calibration(pairs)
    # bin 0.9-1.0: acc 0.9, conf 0.95 -> |0.05| * 10/14 ; bin 0.5-0.6: acc 0.5, conf 0.55 -> 0.05 * 4/14
    assert cal["ece"] == pytest.approx(0.05)
    assert [row["n"] for row in cal["table"]] == [4, 10]
    assert M.calibration([])["ece"] is None


def test_automation_tradeoff() -> None:
    invoices = [(0.95, False, True), (0.9, False, False), (0.7, False, True), (0.99, True, True)]
    rows = {row["threshold"]: row for row in M.automation_tradeoff(invoices, [0.5, 0.92])}
    assert (rows[0.5]["auto_approved"]["k"], rows[0.5]["correct_among_auto"]["k"]) == (3, 2)
    assert (rows[0.92]["auto_approved"]["k"], rows[0.92]["correct_among_auto"]["k"]) == (1, 1)


def test_success_failure_and_expected_failure_definitions() -> None:
    docs = [
        {"valid": True, "status": "completed", "error_code": None, "expected_error_code": None},
        {"valid": True, "status": "needs_review", "error_code": None, "expected_error_code": None},
        {"valid": True, "status": "failed", "error_code": "LLM_ERROR", "expected_error_code": None},
        {"valid": True, "status": "processing", "error_code": None, "expected_error_code": None},  # timed out
        {"valid": False, "status": "rejected", "error_code": "EMPTY_FILE", "expected_error_code": "EMPTY_FILE"},
        {"valid": False, "status": "failed", "error_code": "CORRUPT_FILE", "expected_error_code": "PASSWORD_PROTECTED"},
    ]
    rel = M.reliability(docs)
    assert (rel["success_rate"]["k"], rel["success_rate"]["n"]) == (2, 4)
    assert (rel["failure_rate"]["k"], rel["failure_rate"]["n"]) == (2, 4)
    assert (rel["expected_failure_handling"]["k"], rel["expected_failure_handling"]["n"]) == (1, 2)


def test_search_metrics() -> None:
    ranked = ["d3", "d1", "d9", "d2"]
    assert M.recall_at(ranked, {"d1", "d2"}, 2) == 0.5
    assert M.recall_at(ranked, {"d1", "d2"}, 10) == 1.0
    assert M.reciprocal_rank(ranked, {"d1"}) == 0.5
    assert M.reciprocal_rank(ranked, {"x"}) == 0.0


def test_rule_scores_are_file_level() -> None:
    files = [({"TOTAL_MISMATCH"}, {"TOTAL_MISMATCH"}), (set(), {"TOTAL_MISMATCH"}), ({"TOTAL_MISMATCH"}, set())]
    scores = M.rule_scores(files, ["TOTAL_MISMATCH"])["TOTAL_MISMATCH"]
    assert (scores["tp"], scores["fp"], scores["fn"]) == (1, 1, 1)
