"""Planted-error fixtures for measuring validation precision/recall (Step 10).

Each fixture is a validation context with exactly ONE planted problem (or none,
for the clean ones) and the rule code it must trigger. `evaluate()` returns
expected vs detected codes per fixture:

    from tests.validation.planted import evaluate
    results = evaluate()   # {name: {"expected": {...}, "detected": {...}, "clean": bool}}
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.models.enums import RuleCode, Severity
from app.validation.rules import run_rules
from app.validation.types import SourceKind, ValidationContext
from tests.validation.builders import TODAY, D, candidate, context, document, line, snapshot, with_meta


@dataclass(frozen=True, slots=True)
class PlantedFixture:
    name: str
    description: str
    build: Callable[[], ValidationContext]
    expected: frozenset[RuleCode]  # empty = clean fixture: must produce no ERROR issues

    @property
    def clean(self) -> bool:
        return not self.expected


def _duplicate() -> ValidationContext:
    earlier, later = document(created=0), document(created=1)
    return context(snapshot(), doc=later, candidates=(candidate(snapshot(), earlier),))


def _ledger_mismatch() -> ValidationContext:
    ledger = document(SourceKind.SPREADSHEET, name="ledger.xlsx")
    return context(snapshot(), candidates=(candidate(snapshot(total=D("275.00"), line_items=()), ledger),))


def _ungrounded_number() -> ValidationContext:
    extraction = snapshot(invoice_number=None, invoice_number_norm=None)
    return context(with_meta(extraction, "invoice_number", rejected_value="INV-7777", reason="ungrounded"))


FIXTURES: Final[tuple[PlantedFixture, ...]] = (
    # --- clean: no errors allowed ---
    PlantedFixture("clean_invoice", "consistent EUR invoice with two lines", context, frozenset()),
    PlantedFixture(
        "clean_receipt",
        "receipt without invoice number or customer",
        lambda: context(
            snapshot(
                document_type="receipt", invoice_number=None, invoice_number_norm=None, customer_name=None
            )
        ),
        frozenset(),
    ),
    PlantedFixture(
        "clean_credit_note",
        "credit note with consistent negative amounts",
        lambda: context(
            snapshot(
                document_type="credit_note",
                subtotal=D("-40.00"),
                tax=D("-8.00"),
                total=D("-48.00"),
                line_items=(line(0, "-2", "20.00", "-40.00"),),
            )
        ),
        frozenset(),
    ),
    PlantedFixture(
        "clean_tax_inclusive",
        "line prices include tax (info only)",
        lambda: context(
            snapshot(line_items=(line(0, "1", "150.00", "150.00"), line(1, "1", "120.00", "120.00")))
        ),
        frozenset(),
    ),
    PlantedFixture(
        "clean_jpy",
        "zero-decimal currency",
        lambda: context(
            snapshot(
                currency="JPY",
                subtotal=D("10000"),
                tax=D("1000"),
                total=D("11000"),
                line_items=(line(0, "2", "5000", "10000"),),
            )
        ),
        frozenset(),
    ),
    PlantedFixture(
        "clean_kwd",
        "three-decimal currency",
        lambda: context(
            snapshot(
                currency="KWD",
                subtotal=D("12.345"),
                tax=D("0.617"),
                total=D("12.962"),
                line_items=(line(0, "1", "12.345", "12.345"),),
            )
        ),
        frozenset(),
    ),
    # --- planted errors ---
    PlantedFixture(
        "wrong_total",
        "total != subtotal + tax",
        lambda: context(snapshot(total=D("290.00"))),
        frozenset({RuleCode.TOTAL_MISMATCH}),
    ),
    PlantedFixture(
        "line_sum_off",
        "lines do not add up to the subtotal",
        lambda: context(
            snapshot(line_items=(line(0, "10", "12.50", "125.00"), line(1, "4", "20.00", "80.00")))
        ),
        frozenset({RuleCode.LINE_SUM_MISMATCH}),
    ),
    PlantedFixture(
        "line_amount_off",
        "qty x unit price != line amount (sum still right)",
        lambda: context(
            snapshot(line_items=(line(0, "10", "12.50", "115.00"), line(1, "5", "22.00", "110.00")))
        ),
        frozenset({RuleCode.LINE_AMOUNT_MISMATCH}),
    ),
    PlantedFixture(
        "missing_invoice_number",
        "no invoice number on an invoice",
        lambda: context(snapshot(invoice_number=None, invoice_number_norm=None)),
        frozenset({RuleCode.MISSING_FIELD}),
    ),
    PlantedFixture(
        "ungrounded_number",
        "model's invoice number not in the text",
        _ungrounded_number,
        frozenset({RuleCode.UNGROUNDED_VALUE}),
    ),
    PlantedFixture(
        "future_date",
        "issued well after today",
        lambda: context(snapshot(invoice_date=date(TODAY.year, 12, 1))),
        frozenset({RuleCode.FUTURE_DATE}),
    ),
    PlantedFixture(
        "negative_invoice",
        "negative total on an invoice",
        lambda: context(snapshot(subtotal=D("-225.00"), tax=D("-45.00"), total=D("-270.00"), line_items=())),
        frozenset({RuleCode.NEGATIVE_AMOUNT}),
    ),
    PlantedFixture(
        "unknown_currency",
        "not an ISO 4217 code",
        lambda: context(snapshot(currency="XYZ")),
        frozenset({RuleCode.UNKNOWN_CURRENCY}),
    ),
    PlantedFixture(
        "invalid_date",
        "date printed but unreadable",
        lambda: context(
            with_meta(
                snapshot(invoice_date=None), "invoice_date", rejected_value="31/02/2024", reason="unparseable"
            )
        ),
        frozenset({RuleCode.INVALID_DATE}),
    ),
    PlantedFixture(
        "bad_tax_rate",
        "tax is 80% of the subtotal",
        lambda: context(snapshot(subtotal=D("100.00"), tax=D("80.00"), total=D("180.00"), line_items=())),
        frozenset({RuleCode.IMPLAUSIBLE_TAX_RATE}),
    ),
    PlantedFixture(
        "duplicate_copy", "same invoice uploaded twice", _duplicate, frozenset({RuleCode.DUPLICATE_INVOICE})
    ),
    PlantedFixture(
        "ledger_mismatch",
        "PDF total differs from the Excel ledger",
        _ledger_mismatch,
        frozenset({RuleCode.CROSS_DOC_MISMATCH}),
    ),
)


def detected_rule_codes(fixture: PlantedFixture) -> set[RuleCode]:
    return {issue.rule_code for issue in run_rules(fixture.build())}


def detected_errors(fixture: PlantedFixture) -> set[RuleCode]:
    return {i.rule_code for i in run_rules(fixture.build()) if i.severity is Severity.ERROR}


def evaluate() -> dict[str, dict[str, object]]:
    """Expected vs detected rule codes per fixture (input for precision/recall)."""
    return {
        f.name: {"expected": set(f.expected), "detected": detected_rule_codes(f), "clean": f.clean}
        for f in FIXTURES
    }


__all__ = ["FIXTURES", "PlantedFixture", "detected_errors", "detected_rule_codes", "evaluate"]
