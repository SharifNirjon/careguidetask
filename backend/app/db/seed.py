"""Seed a development database with a small, realistic dataset.

    cd backend && python -m app.db.seed

Creates:
- a 3-page PDF with two invoices (invoice 0 extracted twice -> versions 1 and 2),
  line items, an open validation issue, a manual correction, a job + events;
- an XLSX export containing the same invoice (reconciled against the PDF);
- an exact re-upload of the PDF (same sha256, flagged via duplicate_of_document_id);
- search chunks with deterministic pseudo-embeddings (no ML model needed).

Idempotent: does nothing if the seed documents already exist.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import EMBEDDING_DIM, EMBEDDING_MODEL_DEFAULT, EMBEDDING_SPACE_DEFAULT
from app.db.session import session_scope
from app.db.versioning import add_extraction_version
from app.models import (
    Chunk,
    Correction,
    Document,
    DocumentStatus,
    DocumentUnit,
    EventStatus,
    ExtractionMethod,
    JobStatus,
    JobType,
    LineItem,
    MatchMethod,
    ProcessingEvent,
    ProcessingJob,
    Reconciliation,
    ReconciliationStatus,
    ReviewStatus,
    RuleCode,
    Severity,
    Stage,
    UnitType,
    ValidationIssue,
)

SEED_PREFIX = "seed/"


def fake_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic unit vector derived from the text (stand-in for a real model)."""
    raw = b""
    counter = 0
    while len(raw) < dim:
        raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
        counter += 1
    vec = [(b - 127.5) / 127.5 for b in raw[:dim]]
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def _field_meta(conf: str, evidence: str, unit: int) -> dict:
    return {
        "confidence": conf,
        "evidence_text": evidence,
        "unit_index": unit,
        "source": "llm",
        "grounded": True,
    }


def seed(session: Session) -> bool:
    exists = session.scalar(select(Document.id).where(Document.storage_key.startswith(SEED_PREFIX)).limit(1))
    if exists:
        return False

    now = datetime.now(UTC)
    pdf_sha = hashlib.sha256(b"seed-acme-invoices.pdf").hexdigest()

    # ---- PDF with two invoices -------------------------------------------------
    pdf = Document(
        original_filename="acme_invoices_march.pdf",
        storage_key=f"{SEED_PREFIX}acme_invoices_march.pdf",
        mime_type="application/pdf",
        file_ext="pdf",
        size_bytes=182_344,
        sha256=pdf_sha,
        status=DocumentStatus.NEEDS_REVIEW,
        page_count=3,
        uploaded_by="seed",
        processing_started_at=now,
        processing_finished_at=now,
    )
    session.add(pdf)
    session.flush()

    page_texts = [
        "ACME Supplies Ltd. INVOICE INV-2024-001 Date 2024-03-05 Bill to: Northwind Clinic "
        "Gloves (box) 10 x 12.50 = 125.00  Masks 5 x 20.00 = 100.00 Subtotal 225.00 "
        "VAT 20% 45.00 Total 270.00 EUR",
        "Payment terms: 30 days. Bank: DE89 3704 0044 0532 0130 00",
        "ACME Supplies Ltd. CREDIT NOTE INV-2024-002 Date 2024-03-09 Bill to: Northwind Clinic "
        "Returned masks -2 x 20.00 = -40.00 Total -48.00 EUR",
    ]
    units = [
        DocumentUnit(
            document_id=pdf.id,
            unit_type=UnitType.PDF_PAGE,
            unit_index=i,
            text=t,
            extraction_method=ExtractionMethod.DIGITAL if i < 2 else ExtractionMethod.OCR,
            ocr_confidence=None if i < 2 else Decimal("0.912"),
            image_quality=None if i < 2 else {"dpi": 200, "skew_deg": 1.2, "blur_score": 0.08},
        )
        for i, t in enumerate(page_texts)
    ]
    session.add_all(units)

    job = ProcessingJob(
        document_id=pdf.id,
        job_type=JobType.FULL,
        celery_task_id="seed-task-1",
        status=JobStatus.SUCCEEDED,
        triggered_by="seed",
        started_at=now,
        finished_at=now,
    )
    session.add(job)
    session.flush()
    for stage, ms in [
        (Stage.INGEST, 40),
        (Stage.PARSE, 310),
        (Stage.OCR, 2_400),
        (Stage.SEGMENT, 25),
        (Stage.EXTRACT, 5_800),
        (Stage.VALIDATE, 30),
        (Stage.EMBED, 900),
    ]:
        session.add(
            ProcessingEvent(
                job_id=job.id,
                document_id=pdf.id,
                stage=stage,
                status=EventStatus.SUCCEEDED,
                duration_ms=ms,
                details={"seed": True},
            )
        )

    common = dict(
        supplier_name="ACME Supplies Ltd.",
        supplier_name_norm="acme supplies",
        customer_name="Northwind Clinic",
        currency="EUR",
        model_name="claude-sonnet-5-5",
        token_usage={"input": 2_100, "output": 450},
        latency_ms=5_800,
    )

    # Invoice 0, run 1 (LLM misread the tax), then a re-extraction -> version 2.
    add_extraction_version(
        session,
        document_id=pdf.id,
        invoice_index=0,
        unit_start=0,
        unit_end=1,
        invoice_number="INV-2024-001",
        invoice_number_norm="inv2024001",
        invoice_date=date(2024, 3, 5),
        subtotal=Decimal("225.00"),
        tax=Decimal("54.00"),
        total=Decimal("270.00"),
        overall_confidence=Decimal("0.710"),
        prompt_version="extract-v1",
        review_status=ReviewStatus.NEEDS_REVIEW,
        **common,
    )
    inv0 = add_extraction_version(
        session,
        document_id=pdf.id,
        invoice_index=0,
        unit_start=0,
        unit_end=1,
        invoice_number="INV-2024-001",
        invoice_number_norm="inv2024001",
        invoice_date=date(2024, 3, 5),
        subtotal=Decimal("225.00"),
        tax=Decimal("45.00"),
        total=Decimal("270.00"),
        overall_confidence=Decimal("0.940"),
        prompt_version="extract-v2",
        review_status=ReviewStatus.NEEDS_REVIEW,
        field_meta={
            "invoice_number": _field_meta("0.990", "INVOICE INV-2024-001", 0),
            "total": _field_meta("0.970", "Total 270.00 EUR", 0),
        },
        **common,
    )
    inv0.line_items = [
        LineItem(
            position=0,
            description="Gloves (box)",
            quantity=Decimal("10"),
            unit_price=Decimal("12.50"),
            amount=Decimal("125.00"),
            tax_rate=Decimal("0.2000"),
        ),
        LineItem(
            position=1,
            description="Masks",
            quantity=Decimal("5"),
            unit_price=Decimal("20.00"),
            amount=Decimal("100.00"),
            tax_rate=Decimal("0.2000"),
        ),
    ]

    # Invoice 1: a credit note (negative total is valid).
    inv1 = add_extraction_version(
        session,
        document_id=pdf.id,
        invoice_index=1,
        unit_start=2,
        unit_end=2,
        invoice_number="INV-2024-002",
        invoice_number_norm="inv2024002",
        invoice_date=date(2024, 3, 9),
        subtotal=Decimal("-40.00"),
        tax=None,
        total=Decimal("-48.00"),
        overall_confidence=Decimal("0.820"),
        prompt_version="extract-v2",
        review_status=ReviewStatus.NEEDS_REVIEW,
        **common,
    )
    session.add(
        ValidationIssue(
            extraction_id=inv1.id,
            rule_code=RuleCode.MISSING_FIELD,
            severity=Severity.WARNING,
            field_path="tax",
            message="Tax amount not found on credit note",
        )
    )
    session.add(
        ValidationIssue(
            extraction_id=inv1.id,
            rule_code=RuleCode.TOTAL_MISMATCH,
            severity=Severity.ERROR,
            field_path="total",
            message="subtotal + tax != total",
            expected_value="-40.0000",
            actual_value="-48.0000",
        )
    )

    # Reviewer fills in the missing tax; correction is logged, field_meta marks it manual.
    inv1.tax = Decimal("-8.00")
    inv1.field_meta = {
        **inv1.field_meta,
        "tax": {"source": "manual", "grounded": True, "confidence": "1.000"},
    }
    session.add(
        Correction(
            extraction_id=inv1.id,
            field_path="tax",
            old_value=None,
            new_value="-8.0000",
            corrected_by="reviewer@example.com",
            reason="VAT visible on scanned page footer",
        )
    )

    # ---- XLSX export containing the same invoice ---------------------------------
    xlsx = Document(
        original_filename="erp_export_march.xlsx",
        storage_key=f"{SEED_PREFIX}erp_export_march.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        file_ext="xlsx",
        size_bytes=9_812,
        sha256=hashlib.sha256(b"seed-erp-export.xlsx").hexdigest(),
        status=DocumentStatus.COMPLETED,
        uploaded_by="seed",
    )
    session.add(xlsx)
    session.flush()
    sheet = DocumentUnit(
        document_id=xlsx.id,
        unit_type=UnitType.SHEET,
        unit_index=0,
        sheet_name="March",
        extraction_method=ExtractionMethod.SPREADSHEET,
        text="INV-2024-001 | ACME Supplies Ltd | 2024-03-05 | 225.00 | 45.00 | 275.00 | EUR",
        structured_data={
            "columns": ["invoice", "supplier", "date", "net", "vat", "gross", "ccy"],
            "rows": [["INV-2024-001", "ACME Supplies Ltd", "2024-03-05", "225.00", "45.00", "275.00", "EUR"]],
        },
    )
    session.add(sheet)
    xinv = add_extraction_version(
        session,
        document_id=xlsx.id,
        invoice_index=0,
        unit_start=0,
        unit_end=0,
        invoice_number="INV-2024-001",
        invoice_number_norm="inv2024001",
        supplier_name="ACME Supplies Ltd",
        supplier_name_norm="acme supplies",
        invoice_date=date(2024, 3, 5),
        subtotal=Decimal("225.00"),
        tax=Decimal("45.00"),
        total=Decimal("275.00"),
        currency="EUR",
        overall_confidence=Decimal("1.000"),
        review_status=ReviewStatus.AUTO_APPROVED,
    )
    diffs = [{"field": "total", "left": "270.0000", "right": "275.0000"}]
    session.add(
        Reconciliation.between(
            inv0.id,
            xinv.id,
            differences=diffs,
            match_key="acme supplies|inv2024001",
            match_method=MatchMethod.INVOICE_NUMBER,
            status=ReconciliationStatus.MISMATCHED,
        )
    )
    session.add(
        ValidationIssue(
            extraction_id=inv0.id,
            related_extraction_id=xinv.id,
            rule_code=RuleCode.CROSS_DOC_MISMATCH,
            severity=Severity.ERROR,
            field_path="total",
            message="Total differs from ERP export",
            expected_value="275.0000",
            actual_value="270.0000",
        )
    )

    # ---- exact re-upload of the PDF (stored, flagged as duplicate) ---------------
    session.add(
        Document(
            original_filename="acme_invoices_march (1).pdf",
            storage_key=f"{SEED_PREFIX}acme_invoices_march_copy.pdf",
            mime_type="application/pdf",
            file_ext="pdf",
            size_bytes=182_344,
            sha256=pdf_sha,
            status=DocumentStatus.COMPLETED,
            duplicate_of_document_id=pdf.id,
            uploaded_by="seed",
        )
    )

    # ---- search chunks -----------------------------------------------------------
    session.flush()
    chunk_specs = [(units[0], inv0), (units[1], inv0), (units[2], inv1), (sheet, xinv)]
    for i, (unit, ext) in enumerate(chunk_specs):
        session.add(
            Chunk(
                document_id=unit.document_id,
                unit_id=unit.id,
                extraction_id=ext.id,
                chunk_index=i if unit.document_id == pdf.id else 0,
                content=unit.text,
                token_count=len(unit.text.split()),
                embedding=fake_embedding(unit.text),
                embedding_model=EMBEDDING_MODEL_DEFAULT,
                embedding_space=EMBEDDING_SPACE_DEFAULT,
            )
        )
    return True


def main() -> None:
    with session_scope() as session:
        created = seed(session)
    print("seeded" if created else "seed data already present; nothing to do")


if __name__ == "__main__":
    main()
