"""Acceptance: every planted error is caught; clean fixtures raise no errors."""

from __future__ import annotations

import pytest

from tests.validation.planted import FIXTURES, PlantedFixture, detected_errors, detected_rule_codes, evaluate

PLANTED = [f for f in FIXTURES if not f.clean]
CLEAN = [f for f in FIXTURES if f.clean]


@pytest.mark.parametrize("fixture", PLANTED, ids=lambda f: f.name)
def test_planted_error_is_flagged(fixture: PlantedFixture) -> None:
    assert fixture.expected <= detected_rule_codes(fixture), fixture.description


@pytest.mark.parametrize("fixture", PLANTED, ids=lambda f: f.name)
def test_planted_fixture_raises_nothing_unrelated(fixture: PlantedFixture) -> None:
    """Precision: one planted problem must not set off unrelated errors."""
    assert detected_errors(fixture) <= fixture.expected


@pytest.mark.parametrize("fixture", CLEAN, ids=lambda f: f.name)
def test_clean_fixture_has_no_errors(fixture: PlantedFixture) -> None:
    assert detected_errors(fixture) == set(), fixture.description


def test_evaluate_reports_every_fixture() -> None:
    results = evaluate()
    assert set(results) == {f.name for f in FIXTURES}
    assert all(r["expected"] <= r["detected"] for r in results.values())  # type: ignore[operator]
