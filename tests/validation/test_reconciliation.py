"""Batch reconciliation: pure matcher + the persisted run (real Postgres)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import ValidationSettings
from app.models import Reconciliation, ReconciliationRun, RunStatus
from app.validation.reconciliation import Entry, ReconciliationService, match_sets
from app.validation.types import MatchKind, SourceKind
from tests.validation.builders import D, document, snapshot
from tests.validation.dbseed import XLSX, add_document, add_invoice

SETTINGS = ValidationSettings()


def entry(kind: SourceKind = SourceKind.DOCUMENT, **fields: object) -> Entry:
    return Entry(snapshot(**fields), document(kind))  # type: ignore[arg-type]


def test_report_covers_every_bucket() -> None:
    pdfs = [
        entry(invoice_number="A-1", invoice_number_norm="A1"),
        entry(invoice_number="B-2", invoice_number_norm="B2"),
        entry(invoice_number="C-3", invoice_number_norm="C3"),  # nothing in the ledger
        entry(invoice_number="D-4", invoice_number_norm="D4"),  # ledger has it twice
    ]
    ledger = [
        entry(
            SourceKind.SPREADSHEET, invoice_number="A-1", invoice_number_norm="A1", tax=None, line_items=()
        ),
        entry(
            SourceKind.SPREADSHEET,
            invoice_number="B-2",
            invoice_number_norm="B2",
            total=D("275.00"),
            line_items=(),
        ),
        entry(SourceKind.SPREADSHEET, invoice_number="D-4", invoice_number_norm="D4", line_items=()),
        entry(SourceKind.SPREADSHEET, invoice_number="D-4", invoice_number_norm="D4", line_items=()),
        entry(
            SourceKind.SPREADSHEET,
            invoice_number="Z-9",
            invoice_number_norm="Z9",
            supplier_name_norm="globex",
            total=D("10.00"),
        ),  # only in the ledger
    ]
    report = match_sets(pdfs, ledger, SETTINGS)
    assert [p.left.extraction.invoice_number for p in report.matched] == ["A-1"]
    assert [p.left.extraction.invoice_number for p in report.mismatched] == ["B-2"]
    assert [d.to_dict() for d in report.mismatched[0].comparison.differences] == [
        {"field": "total", "left": "270.00", "right": "275.00"}
    ]
    assert [e.extraction.invoice_number for e in report.only_in_left] == ["C-3"]
    assert [e.extraction.invoice_number for e in report.only_in_right] == ["Z-9"]
    assert len(report.ambiguous) == 1 and len(report.ambiguous[0]["right"]) == 2
    assert report.summary()["counts"] == {
        "matched": 1,
        "mismatched": 1,
        "only_in_left": 1,
        "only_in_right": 1,
        "ambiguous": 1,
    }


def test_two_lefts_claiming_one_right_is_ambiguous() -> None:
    left = [entry(), entry()]  # the same invoice uploaded twice on the left
    report = match_sets(left, [entry(SourceKind.SPREADSHEET)], SETTINGS)
    assert report.matched == [] and len(report.ambiguous) == 1
    assert len(report.ambiguous[0]["left"]) == 2


def test_fuzzy_pairing_when_numbers_differ() -> None:
    left = [entry(invoice_number="REF 77", invoice_number_norm="REF77")]
    right = [
        entry(
            SourceKind.SPREADSHEET,
            invoice_number=None,
            invoice_number_norm=None,
            invoice_date=date(2024, 3, 6),
        )
    ]
    report = match_sets(left, right, SETTINGS)
    pair = report.mismatched[0]  # paired fuzzily, and the one-day date gap is reported, not hidden
    assert pair.kind is MatchKind.FUZZY
    assert [d.field for d in pair.comparison.differences] == ["invoice_date"]


def test_supplier_must_be_similar() -> None:
    report = match_sets([entry()], [entry(SourceKind.SPREADSHEET, supplier_name_norm="globex")], SETTINGS)
    assert len(report.only_in_left) == len(report.only_in_right) == 1


@pytest.fixture
def sessions(committed_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=committed_engine, expire_on_commit=False)


def test_run_is_persisted_with_linked_reconciliations(sessions: sessionmaker[Session]) -> None:
    with sessions() as session, session.begin():
        pdf_a, pdf_b = add_document(session, name="a.pdf"), add_document(session, name="b.pdf")
        ledger = add_document(session, name="ledger.xlsx", mime=XLSX)
        add_invoice(session, pdf_a, number="A-1")
        add_invoice(session, pdf_b, number="B-2", issued=date(2024, 3, 20))
        add_invoice(session, ledger, number="A-1", lines=[])
        add_invoice(
            session, ledger, invoice_index=1, number="B-2", total="275.00", lines=[], issued=date(2024, 3, 20)
        )
        left_ids, right_ids = [pdf_a.id, pdf_b.id], [ledger.id]

    run_id, report = ReconciliationService(session_factory=sessions, settings=SETTINGS).run(
        left_ids, right_ids, "auditor@example.com"
    )
    assert (len(report.matched), len(report.mismatched)) == (1, 1)
    with sessions() as session:
        run = session.get_one(ReconciliationRun, run_id)
        rows = session.scalars(select(Reconciliation).where(Reconciliation.run_id == run_id)).all()
    assert run.status is RunStatus.SUCCEEDED and run.finished_at is not None
    assert run.created_by == "auditor@example.com" and run.summary["counts"]["mismatched"] == 1
    assert sorted(str(r.status) for r in rows) == ["matched", "mismatched"]


def test_failed_run_is_recorded(sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    from app.validation import reconciliation

    def explode(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(reconciliation, "match_sets", explode)
    service = ReconciliationService(session_factory=sessions, settings=SETTINGS)
    with pytest.raises(RuntimeError):
        service.run([uuid.uuid4()], [uuid.uuid4()], None)
    with sessions() as session:
        run = session.scalars(select(ReconciliationRun)).one()
    assert run.status is RunStatus.FAILED and run.summary == {"error": "RuntimeError"}
