"""Row-level integrity: CHECKs, uniqueness, FKs, triggers, cascades, exact money."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import (
    Chunk,
    Correction,
    Document,
    DocumentStatus,
    ErrorCode,
    EventStatus,
    Extraction,
    JobType,
    LineItem,
    MatchMethod,
    ProcessingEvent,
    ProcessingJob,
    Reconciliation,
    ReconciliationStatus,
    RuleCode,
    Severity,
    Stage,
    ValidationIssue,
)
from tests.db.factories import make_document, make_extraction, make_unit


def expect_violation(db: Session, constraint: str, action: Callable[[], object]) -> None:
    """Run `action` in a SAVEPOINT; assert it fails on exactly `constraint`."""
    with pytest.raises(IntegrityError) as info:
        with db.begin_nested():
            action()
            db.flush()
    diag = info.value.orig.diag  # type: ignore[union-attr]
    assert constraint in (diag.constraint_name, diag.message_primary), diag.message_primary


# ------------------------------------------------------------------ documents


def test_duplicate_uploads_are_stored_and_flagged(db: Session) -> None:
    original = make_document(db)
    dup = make_document(db, sha256=original.sha256, duplicate_of_document_id=original.id)
    assert dup.duplicate_of is original
    same_hash = db.scalar(select(func.count()).where(Document.sha256 == original.sha256))
    assert same_hash == 2


def test_storage_key_is_unique(db: Session) -> None:
    doc = make_document(db)
    expect_violation(
        db, "uq_documents_storage_key", lambda: make_document(db, storage_key=doc.storage_key)
    )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"size_bytes": 0}, "ck_documents_size_bytes_positive"),
        ({"sha256": "A" * 64}, "ck_documents_sha256_hex"),
        ({"status": DocumentStatus.FAILED}, "ck_documents_failed_has_error_code"),
        ({"page_count": -1}, "ck_documents_page_count_nonneg"),
    ],
)
def test_document_checks(db: Session, overrides: dict, constraint: str) -> None:
    expect_violation(db, constraint, lambda: make_document(db, **overrides))


def test_failed_document_with_error_code_is_valid(db: Session) -> None:
    doc = make_document(db, status=DocumentStatus.FAILED, error_code=ErrorCode.PASSWORD_PROTECTED)
    assert doc.error_code is ErrorCode.PASSWORD_PROTECTED


def test_document_cannot_be_duplicate_of_itself(db: Session) -> None:
    doc = make_document(db)

    def act() -> None:
        doc.duplicate_of_document_id = doc.id

    expect_violation(db, "ck_documents_not_duplicate_of_self", act)


def test_db_rejects_unknown_enum_value_even_via_raw_sql(db: Session) -> None:
    doc = make_document(db)
    expect_violation(
        db,
        "ck_documents_status",
        lambda: db.execute(
            text("UPDATE documents SET status = 'bogus' WHERE id = :id"), {"id": doc.id}
        ),
    )


def test_orm_rejects_unknown_enum_value_before_sql(db: Session) -> None:
    with pytest.raises(StatementError):
        with db.begin_nested():
            make_document(db, status="bogus")


def test_enum_columns_load_as_python_enums(db: Session) -> None:
    doc = make_document(db)
    db.expire(doc)
    assert doc.status is DocumentStatus.UPLOADED


def test_updated_at_is_maintained_by_trigger_even_for_raw_sql(db: Session) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    doc = make_document(db, created_at=old, updated_at=old)
    db.execute(text("UPDATE documents SET status = 'queued' WHERE id = :id"), {"id": doc.id})
    db.refresh(doc)
    assert doc.created_at == old
    assert doc.updated_at > old


# ------------------------------------------------------------- document_units


def test_unit_position_is_unique_per_document(db: Session) -> None:
    doc = make_document(db)
    make_unit(db, doc, 0)
    expect_violation(
        db, "uq_document_units_document_id_unit_type_unit_index", lambda: make_unit(db, doc, 0)
    )


def test_unit_index_and_ocr_confidence_bounds(db: Session) -> None:
    doc = make_document(db)
    expect_violation(db, "ck_document_units_unit_index_nonneg", lambda: make_unit(db, doc, -1))
    expect_violation(
        db,
        "ck_document_units_ocr_confidence_range",
        lambda: make_unit(db, doc, 1, ocr_confidence=Decimal("1.001")),
    )


# ---------------------------------------------------------------- extractions


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"currency": "eur"}, "ck_extractions_currency_iso4217"),
        ({"currency": "E1R"}, "ck_extractions_currency_iso4217"),
        ({"overall_confidence": Decimal("1.5")}, "ck_extractions_overall_confidence_range"),
        ({"overall_confidence": Decimal("-0.1")}, "ck_extractions_overall_confidence_range"),
        ({"unit_start": 3, "unit_end": 2}, "ck_extractions_unit_range"),
        ({"version": 0}, "ck_extractions_version_positive"),
    ],
)
def test_extraction_checks(db: Session, overrides: dict, constraint: str) -> None:
    doc = make_document(db)
    expect_violation(db, constraint, lambda: make_extraction(db, doc, **overrides))


def test_missing_values_stay_null_and_credit_notes_may_be_negative(db: Session) -> None:
    doc = make_document(db)
    ext = make_extraction(db, doc, total=Decimal("-48.00"), currency="EUR")
    db.expire(ext)
    assert ext.total == Decimal("-48.0000")
    assert ext.invoice_number is None and ext.invoice_date is None and ext.tax is None


def test_only_one_current_version_per_invoice(db: Session) -> None:
    doc = make_document(db)
    make_extraction(db, doc, version=1)
    expect_violation(
        db,
        "uq_extractions_document_id_invoice_index_current",
        lambda: make_extraction(db, doc, version=2, is_current=True),
    )


def test_version_numbers_are_unique_per_invoice(db: Session) -> None:
    doc = make_document(db)
    make_extraction(db, doc, version=1)
    expect_violation(
        db,
        "uq_extractions_document_id_invoice_index_version",
        lambda: make_extraction(db, doc, version=1, is_current=False),
    )


def test_money_is_exact_decimal(db: Session) -> None:
    doc = make_document(db)
    big = Decimal("99999999999999.9999")  # max for NUMERIC(18,4)
    ext = make_extraction(db, doc, subtotal=Decimal("0.1"), tax=Decimal("0.2"), total=big)
    db.expire(ext)
    assert isinstance(ext.total, Decimal) and ext.total == big
    exact = db.scalar(select(Extraction.subtotal + Extraction.tax).where(Extraction.id == ext.id))
    assert exact == Decimal("0.3")


# ----------------------------------------------------- line items / validation


def test_line_item_positions_unique(db: Session) -> None:
    ext = make_extraction(db, make_document(db))
    db.add(LineItem(extraction_id=ext.id, position=0, amount=Decimal("1")))
    db.flush()
    expect_violation(
        db,
        "uq_line_items_extraction_id_position",
        lambda: db.add(LineItem(extraction_id=ext.id, position=0)),
    )


def test_validation_issue_history_and_related_extraction(db: Session) -> None:
    doc = make_document(db)
    a = make_extraction(db, doc, invoice_index=0)
    b = make_extraction(db, doc, invoice_index=1)
    issue = ValidationIssue(
        extraction_id=a.id, related_extraction_id=b.id, rule_code=RuleCode.DUPLICATE_INVOICE,
        severity=Severity.WARNING, message="possible duplicate",
    )
    db.add(issue)
    db.flush()
    assert a.validation_issues == [issue] and issue.related_extraction is b

    def self_ref() -> None:
        issue.related_extraction_id = a.id

    expect_violation(db, "ck_validation_issues_related_not_self", self_ref)


# --------------------------------------------------------- append-only tables


def test_corrections_are_append_only(db: Session) -> None:
    ext = make_extraction(db, make_document(db))
    corr = Correction(
        extraction_id=ext.id, field_path="total", old_value="1.0000", new_value="2.0000",
        corrected_by="reviewer",
    )
    db.add(corr)
    db.flush()

    def tamper() -> None:
        corr.new_value = "3.0000"

    expect_violation(db, "corrections is append-only; UPDATE is not allowed", tamper)


def test_deleting_line_item_keeps_its_correction(db: Session) -> None:
    ext = make_extraction(db, make_document(db))
    item = LineItem(extraction_id=ext.id, position=0, amount=Decimal("5"))
    db.add(item)
    db.flush()
    corr = Correction(
        extraction_id=ext.id, line_item_id=item.id, field_path="line_items[0].amount",
        old_value="5.0000", new_value="6.0000", corrected_by="reviewer",
    )
    db.add(corr)
    db.flush()
    db.execute(text("DELETE FROM line_items WHERE id = :id"), {"id": item.id})
    db.expire_all()
    assert corr.line_item_id is None and corr.field_path == "line_items[0].amount"


def test_json_null_vs_sql_null_in_corrections(db: Session) -> None:
    ext = make_extraction(db, make_document(db))
    corr = Correction(
        extraction_id=ext.id, field_path="tax", old_value=None, new_value={"amount": "8.00"},
        corrected_by="reviewer",
    )
    db.add(corr)
    db.flush()
    is_sql_null = db.scalar(
        text("SELECT old_value IS NULL FROM corrections WHERE id = :id"), {"id": corr.id}
    )
    assert is_sql_null is True


def test_processing_events_are_append_only(db: Session) -> None:
    doc = make_document(db)
    job = ProcessingJob(document_id=doc.id, job_type=JobType.FULL)
    db.add(job)
    db.flush()
    event = ProcessingEvent(
        job_id=job.id, document_id=doc.id, stage=Stage.PARSE, status=EventStatus.STARTED
    )
    db.add(event)
    db.flush()
    assert isinstance(event.id, int)

    def tamper() -> None:
        event.status = EventStatus.SUCCEEDED

    expect_violation(db, "processing_events is append-only; UPDATE is not allowed", tamper)


# ---------------------------------------------- denormalized document_id safety


def test_event_document_must_match_its_job(db: Session) -> None:
    doc_a, doc_b = make_document(db), make_document(db)
    job = ProcessingJob(document_id=doc_a.id, job_type=JobType.FULL)
    db.add(job)
    db.flush()
    expect_violation(
        db,
        "fk_processing_events_job_id_processing_jobs",
        lambda: db.add(
            ProcessingEvent(
                job_id=job.id, document_id=doc_b.id, stage=Stage.PARSE, status=EventStatus.STARTED
            )
        ),
    )
    # Events without a job (e.g. on upload) are fine.
    db.add(ProcessingEvent(document_id=doc_b.id, stage=Stage.INGEST, status=EventStatus.SUCCEEDED))
    db.flush()


def test_chunk_document_must_match_its_unit_and_extraction(db: Session) -> None:
    doc_a, doc_b = make_document(db), make_document(db)
    unit_a = make_unit(db, doc_a)
    unit_b = make_unit(db, doc_b)
    ext_a = make_extraction(db, doc_a)
    expect_violation(
        db,
        "fk_chunks_unit_id_document_units",
        lambda: db.add(Chunk(document_id=doc_b.id, unit_id=unit_a.id, chunk_index=0, content="x")),
    )
    expect_violation(
        db,
        "fk_chunks_extraction_id_extractions",
        lambda: db.add(
            Chunk(
                document_id=doc_b.id, unit_id=unit_b.id, extraction_id=ext_a.id,
                chunk_index=0, content="x",
            )
        ),
    )


def test_chunk_embedding_requires_model_name(db: Session) -> None:
    doc = make_document(db)
    unit = make_unit(db, doc)
    expect_violation(
        db,
        "ck_chunks_embedding_has_model",
        lambda: db.add(
            Chunk(
                document_id=doc.id, unit_id=unit.id, chunk_index=0, content="x",
                embedding=[0.0] * 384,
            )
        ),
    )


# ------------------------------------------------------------ reconciliations


def _reconcile_kw() -> dict:
    return dict(match_key="k", match_method=MatchMethod.INVOICE_NUMBER,
                status=ReconciliationStatus.MATCHED)


def test_reconciliation_pair_is_canonical(db: Session) -> None:
    a = make_extraction(db, make_document(db))
    b = make_extraction(db, make_document(db))
    lo, hi = sorted([a.id, b.id])
    expect_violation(
        db,
        "ck_reconciliations_canonical_pair_order",
        lambda: db.add(
            Reconciliation(left_extraction_id=hi, right_extraction_id=lo, **_reconcile_kw())
        ),
    )
    rec = Reconciliation.between(
        hi, lo, differences=[{"field": "total", "left": "1", "right": "2"}], **_reconcile_kw()
    )
    db.add(rec)
    db.flush()
    assert (rec.left_extraction_id, rec.right_extraction_id) == (lo, hi)
    assert rec.differences == [{"field": "total", "left": "2", "right": "1"}]
    expect_violation(
        db,
        "uq_reconciliations_left_extraction_id_right_extraction_id",
        lambda: db.add(Reconciliation.between(lo, hi, **_reconcile_kw())),
    )


def test_uuid_ordering_matches_postgres(db: Session) -> None:
    ids = [uuid.uuid4() for _ in range(50)]
    pg_sorted = db.scalars(
        text("SELECT u FROM unnest(CAST(:ids AS uuid[])) AS u ORDER BY u"), {"ids": ids}
    ).all()
    assert pg_sorted == sorted(ids)


# ------------------------------------------------------------------- cascades


def test_hard_delete_cascades_through_everything(db: Session) -> None:
    from app.db.seed import seed

    seed(db)
    doc_ids = db.scalars(select(Document.id).where(Document.storage_key.like("seed/%"))).all()
    assert len(doc_ids) == 3
    db.execute(text("DELETE FROM documents WHERE storage_key LIKE 'seed/%'"))
    for table in (
        "document_units", "extractions", "line_items", "validation_issues", "corrections",
        "processing_jobs", "processing_events", "reconciliations", "chunks",
    ):
        assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
