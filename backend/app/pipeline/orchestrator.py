"""Pipeline orchestration: the ONLY pipeline module that touches the database.

Stages, in order:  ingest (intake.py) -> parse -> segment -> extract -> validate -> embed

    begin()        mark processing, validate the file, plan unit batches
    parse_batch()  parse units [start, end) and upsert them (idempotent)
    finalize()     partial-failure rules, then the post-parse stages

Celery runs parse_batch() as a chord over page batches (app/workers/tasks.py);
run_document() runs the same functions inline (CLI --sync, tests).
extract/validate/embed are registered no-op stubs that log `skipped` events;
Steps 4, 5 and 7 replace them in POST_PARSE_STAGES.
"""

from __future__ import annotations

import logging
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import PipelineSettings, get_pipeline_settings
from app.db.session import get_sessionmaker
from app.models import (
    Document,
    DocumentStatus,
    DocumentUnit,
    EventStatus,
    JobStatus,
    ProcessingEvent,
    ProcessingJob,
    Stage,
    UnitType,
)
from app.models.enums import ErrorCode
from app.pipeline import csv_parser, images, pdf, spreadsheet
from app.pipeline.errors import PermanentError, PipelineError
from app.pipeline.ocr.base import OcrEngine
from app.pipeline.ocr.tesseract import TesseractEngine
from app.pipeline.segment import HeuristicSegmenter, Segmenter, segment_document
from app.pipeline.storage import LocalStorage, Storage
from app.pipeline.types import FileKind, ParsedUnit

log = logging.getLogger(__name__)

CONFIDENCE_QUANT = Decimal("0.001")
SCOPE_BATCH = "batch"
SCOPE_DOCUMENT = "document"


# -------------------------------------------------------------- dependencies


@dataclass(slots=True)
class PipelineContext:
    """Everything the orchestrator needs, injectable for tests."""

    session_factory: sessionmaker[Session]
    storage: Storage
    ocr_engine_factory: Callable[[], OcrEngine]
    settings: PipelineSettings
    segmenter: Segmenter = field(default_factory=HeuristicSegmenter)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


@lru_cache(maxsize=1)
def default_context() -> PipelineContext:
    settings = get_pipeline_settings()
    return PipelineContext(
        session_factory=get_sessionmaker(),
        storage=LocalStorage(settings.storage_root),
        ocr_engine_factory=lambda: TesseractEngine(settings.ocr_langs, settings.ocr_page_timeout_s),
        settings=settings,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _log_extra(document_id: uuid.UUID, job_id: uuid.UUID, stage: Stage) -> dict[str, str]:
    return {"document_id": str(document_id), "job_id": str(job_id), "stage": stage.value}


def record_event(
    session: Session,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    stage: Stage,
    status: EventStatus,
    *,
    duration_ms: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        ProcessingEvent(
            document_id=document_id,
            job_id=job_id,
            stage=stage,
            status=status,
            duration_ms=duration_ms,
            details=details or {},
        )
    )


# ----------------------------------------------------------------------- plan


@dataclass(frozen=True, slots=True)
class ParsePlan:
    kind: FileKind
    batches: list[tuple[int, int]]
    file_warnings: list[str] = field(default_factory=list)

    @property
    def needs_ocr_queue(self) -> bool:
        return self.kind is FileKind.PDF or self.kind.is_image

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "batches": self.batches, "file_warnings": self.file_warnings}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParsePlan:
        return cls(FileKind(data["kind"]), [tuple(b) for b in data["batches"]], data["file_warnings"])


def _batches(total: int, size: int) -> list[tuple[int, int]]:
    return [(start, min(start + size, total)) for start in range(0, total, size)]


def _plan(kind: FileKind, path: Path, settings: PipelineSettings) -> tuple[ParsePlan, int | None]:
    """Validate the file and split it into unit batches. Returns (plan, page_count)."""
    if kind is FileKind.PDF:
        info = pdf.inspect_pdf(path, settings)
        return (
            ParsePlan(kind, _batches(info.page_count, settings.page_batch_size), info.warnings),
            info.page_count,
        )
    if kind.is_image:
        frames = images.count_frames(path, settings)
        return ParsePlan(kind, _batches(frames, settings.page_batch_size)), frames
    return ParsePlan(kind, [(0, 1)]), None  # workbooks/CSV: one pass over the whole file


def begin(
    ctx: PipelineContext, document_id: uuid.UUID, job_id: uuid.UUID, attempt: int = 1
) -> ParsePlan | None:
    """Mark the document processing and plan the parse. None = document failed."""
    with ctx.session() as session:
        doc = session.get_one(Document, document_id)
        job = session.get_one(ProcessingJob, job_id)
        doc.status = DocumentStatus.PROCESSING
        doc.error_code = doc.error_message = None
        doc.segments = None  # a re-run must not leave the previous run's boundaries behind
        doc.processing_started_at, doc.processing_finished_at = _now(), None
        job.status, job.started_at, job.attempt = JobStatus.RUNNING, _now(), attempt
        record_event(
            session, document_id, job_id, Stage.PARSE, EventStatus.STARTED, details={"attempt": attempt}
        )
        kind, key = FileKind.from_mime(doc.mime_type), doc.storage_key

    try:
        with ctx.storage.local_path(key) as path:
            plan, page_count = _plan(kind, path, ctx.settings)
    except PermanentError as exc:
        fail(ctx, document_id, job_id, exc, Stage.PARSE)
        return None
    with ctx.session() as session:
        session.get_one(Document, document_id).page_count = page_count
    log.info(
        "parse planned",
        extra={
            **_log_extra(document_id, job_id, Stage.PARSE),
            "batches": len(plan.batches),
            "kind": kind.value,
        },
    )
    return plan


# ---------------------------------------------------------------------- parse


def _parse_units(kind: FileKind, path: Path, start: int, end: int, ctx: PipelineContext) -> list[ParsedUnit]:
    settings = ctx.settings
    if kind is FileKind.PDF:
        return pdf.parse_pdf_pages(path, start, end, engine=ctx.ocr_engine_factory(), settings=settings)
    if kind.is_image:
        return images.parse_image_frames(path, start, end, engine=ctx.ocr_engine_factory(), settings=settings)
    if kind is FileKind.XLSX:
        return spreadsheet.parse_xlsx(path, settings)
    if kind is FileKind.XLS:
        return spreadsheet.parse_xls(path, settings)
    return [csv_parser.parse_csv(path, settings)]


def _unit_row(document_id: uuid.UUID, unit: ParsedUnit) -> dict[str, Any]:
    confidence = (
        Decimal(str(unit.ocr_confidence)).quantize(CONFIDENCE_QUANT)
        if unit.ocr_confidence is not None
        else None
    )
    return {
        "document_id": document_id,
        "unit_type": unit.unit_type,
        "unit_index": unit.unit_index,
        "sheet_name": unit.sheet_name,
        "text": unit.text,
        "extraction_method": unit.extraction_method,
        "ocr_confidence": confidence,
        "structured_data": unit.structured_data,
        "image_quality": unit.image_quality,
        "error_code": unit.error_code,
        "warnings": unit.warnings,
    }


_UPSERT_COLUMNS = (
    "sheet_name",
    "text",
    "extraction_method",
    "ocr_confidence",
    "structured_data",
    "image_quality",
    "error_code",
    "warnings",
)


def upsert_units(session: Session, document_id: uuid.UUID, units: list[ParsedUnit]) -> None:
    """Idempotent bulk write keyed on (document_id, unit_type, unit_index).

    Re-parsing updates rows in place, so unit ids (and anything referencing
    them, e.g. chunks) stay stable and nothing is duplicated.
    """
    if not units:
        return
    stmt = pg_insert(DocumentUnit).values([_unit_row(document_id, u) for u in units])
    stmt = stmt.on_conflict_do_update(
        index_elements=["document_id", "unit_type", "unit_index"],
        set_={col: stmt.excluded[col] for col in _UPSERT_COLUMNS},
    )
    session.execute(stmt)


def _batch_details(start: int, end: int, units: list[ParsedUnit]) -> dict[str, Any]:
    unit_ms: dict[str, list[int]] = {}
    for unit in units:
        unit_ms.setdefault(unit.extraction_method.value, []).append(unit.duration_ms)
    return {
        "scope": SCOPE_BATCH,
        "batch": [start, end],
        "units": len(units),
        "failed": sum(u.is_failed for u in units),
        "methods": dict(Counter(u.extraction_method.value for u in units)),
        "unit_ms": unit_ms,
    }


def parse_batch(
    ctx: PipelineContext, document_id: uuid.UUID, job_id: uuid.UUID, plan: ParsePlan, start: int, end: int
) -> dict[str, Any]:
    """Parse and persist one batch. File-level PermanentErrors propagate."""
    started = time.perf_counter()
    with ctx.session() as session:
        key = session.get_one(Document, document_id).storage_key
    with ctx.storage.local_path(key) as path:
        units = _parse_units(plan.kind, path, start, end, ctx)
    details = _batch_details(start, end, units)
    with ctx.session() as session:
        upsert_units(session, document_id, units)
        record_event(
            session,
            document_id,
            job_id,
            Stage.PARSE,
            EventStatus.SUCCEEDED,
            duration_ms=int((time.perf_counter() - started) * 1000),
            details=details,
        )
    log.info(
        "batch parsed",
        extra={**_log_extra(document_id, job_id, Stage.PARSE), "batch": [start, end], "units": len(units)},
    )
    return details


# ------------------------------------------------------------------- finalize


@dataclass(slots=True)
class UnitStats:
    total: int
    failed: int
    with_text: int
    methods: dict[str, int]
    failed_codes: Counter[ErrorCode]
    ocr_confidences: list[float]
    texts: list[str | None]
    unit_type: UnitType | None


def _unit_stats(session: Session, document_id: uuid.UUID) -> UnitStats:
    rows = session.execute(
        select(
            DocumentUnit.unit_type,
            DocumentUnit.error_code,
            DocumentUnit.text,
            DocumentUnit.extraction_method,
            DocumentUnit.ocr_confidence,
        )
        .where(DocumentUnit.document_id == document_id)
        .order_by(DocumentUnit.unit_type, DocumentUnit.unit_index)
    ).all()
    return UnitStats(
        total=len(rows),
        failed=sum(r.error_code is not None for r in rows),
        with_text=sum(bool(r.text and r.text.strip()) for r in rows),
        methods=dict(Counter(r.extraction_method.value for r in rows)),
        failed_codes=Counter(r.error_code for r in rows if r.error_code is not None),
        ocr_confidences=[float(r.ocr_confidence) for r in rows if r.ocr_confidence is not None],
        texts=[r.text for r in rows],
        unit_type=rows[0].unit_type if rows else None,
    )


def _document_failure(stats: UnitStats, plan: ParsePlan, settings: PipelineSettings) -> PipelineError | None:
    """The document fails only on too many failed units or no text at all."""
    if stats.total and stats.failed / stats.total > settings.max_failed_unit_ratio:
        code = stats.failed_codes.most_common(1)[0][0]
        return PermanentError(code, details={"failed_units": stats.failed, "units": stats.total})
    if stats.with_text == 0:
        repaired = any(pdf.REPAIR_MARKER in w for w in plan.file_warnings)
        return PermanentError(
            ErrorCode.CORRUPT_FILE if repaired else ErrorCode.NO_TEXT_FOUND,
            details={"units": stats.total, "pdf_repaired": repaired},
        )
    return None


def _percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def _parse_summary(
    session: Session, document_id: uuid.UUID, job_id: uuid.UUID, stats: UnitStats
) -> dict[str, Any]:
    batch_events = session.scalars(
        select(ProcessingEvent.details).where(
            ProcessingEvent.job_id == job_id,
            ProcessingEvent.stage == Stage.PARSE,
            ProcessingEvent.status == EventStatus.SUCCEEDED,
        )
    ).all()
    per_method: dict[str, list[int]] = {}
    for details in batch_events:
        if details.get("scope") != SCOPE_BATCH:
            continue
        for method, values in details.get("unit_ms", {}).items():
            per_method.setdefault(method, []).extend(values)
    return {
        "scope": SCOPE_DOCUMENT,
        "units": stats.total,
        "failed": stats.failed,
        "methods": stats.methods,
        "avg_ocr_confidence": (
            round(statistics.fmean(stats.ocr_confidences), 3) if stats.ocr_confidences else None
        ),
        "unit_ms": {
            m: {"p50": _percentile(v, 50), "p95": _percentile(v, 95), "n": len(v)}
            for m, v in per_method.items()
        },
    }


# Post-parse stages. Each returns (status, details). Steps 4/5/7 replace the stubs.
StageFn = Callable[[Session, Document, UnitStats, PipelineContext], tuple[EventStatus, dict[str, Any]]]


def run_segment_stage(
    session: Session, doc: Document, stats: UnitStats, ctx: PipelineContext
) -> tuple[EventStatus, dict[str, Any]]:
    segments = segment_document(stats.unit_type, stats.texts, ctx.segmenter)
    doc.segments = [s.to_dict() for s in segments]
    return EventStatus.SUCCEEDED, {"segments": len(segments), "confidences": [s.confidence for s in segments]}


def not_implemented_stage(step: str) -> StageFn:
    def stub(
        session: Session, doc: Document, stats: UnitStats, ctx: PipelineContext
    ) -> tuple[EventStatus, dict[str, Any]]:
        return EventStatus.SKIPPED, {"reason": f"not implemented yet ({step})"}

    return stub


POST_PARSE_STAGES: list[tuple[Stage, StageFn]] = [
    (Stage.SEGMENT, run_segment_stage),
    (Stage.EXTRACT, not_implemented_stage("Step 4")),
    (Stage.VALIDATE, not_implemented_stage("Step 5")),
    (Stage.EMBED, not_implemented_stage("Step 7")),
]


def finalize(
    ctx: PipelineContext, document_id: uuid.UUID, job_id: uuid.UUID, plan: ParsePlan
) -> DocumentStatus:
    """Apply the partial-failure rules, run post-parse stages, set final status."""
    with ctx.session() as session:
        doc = session.get_one(Document, document_id)
        if doc.status is DocumentStatus.FAILED:
            return doc.status  # a batch already failed the document
        stats = _unit_stats(session, document_id)
        failure = _document_failure(stats, plan, ctx.settings)
        parse_ms = int((_now() - doc.processing_started_at).total_seconds() * 1000)
        if failure is None:
            summary = _parse_summary(session, document_id, job_id, stats)
            summary["file_warnings"] = plan.file_warnings  # e.g. MuPDF repaired the file
            record_event(
                session,
                document_id,
                job_id,
                Stage.PARSE,
                EventStatus.SUCCEEDED,
                duration_ms=parse_ms,
                details=summary,
            )
            doc.page_count = doc.page_count or stats.total
    if failure is not None:
        fail(ctx, document_id, job_id, failure, Stage.PARSE)
        return DocumentStatus.FAILED

    for stage, fn in POST_PARSE_STAGES:
        started = time.perf_counter()
        try:
            with ctx.session() as session:
                doc = session.get_one(Document, document_id)
                status, details = fn(session, doc, stats, ctx)
                record_event(
                    session,
                    document_id,
                    job_id,
                    stage,
                    status,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    details=details,
                )
        except PipelineError as exc:
            fail(ctx, document_id, job_id, exc, stage)
            return DocumentStatus.FAILED

    with ctx.session() as session:
        doc = session.get_one(Document, document_id)
        job = session.get_one(ProcessingJob, job_id)
        doc.status, doc.processing_finished_at = DocumentStatus.COMPLETED, _now()
        job.status, job.finished_at = JobStatus.SUCCEEDED, _now()
    log.info("document completed", extra=_log_extra(document_id, job_id, Stage.EMBED))
    return DocumentStatus.COMPLETED


def fail(
    ctx: PipelineContext, document_id: uuid.UUID, job_id: uuid.UUID, error: PipelineError, stage: Stage
) -> None:
    """Mark document + job failed with a user-safe message; log details internally.

    Idempotent: a job that is already failed is left alone (the chord errback
    and a task's on_failure hook may both report the same failure).
    """
    with ctx.session() as session:
        doc = session.get_one(Document, document_id)
        job = session.get_one(ProcessingJob, job_id)
        if job.status is JobStatus.FAILED:
            return
        now = _now()
        doc.status, doc.error_code, doc.error_message = DocumentStatus.FAILED, error.code, error.message
        doc.processing_finished_at = now
        job.status, job.error_code, job.error_message = JobStatus.FAILED, error.code, error.message
        job.finished_at = now
        record_event(session, document_id, job_id, stage, EventStatus.FAILED, details=error.to_dict())
    log.warning(
        "document failed",
        extra={
            **_log_extra(document_id, job_id, stage),
            "error_code": error.code.value,
            "details": error.details,
        },
    )


# ----------------------------------------------------------------- sync runner


def run_document(ctx: PipelineContext, document_id: uuid.UUID, job_id: uuid.UUID) -> DocumentStatus:
    """Run the whole pipeline inline (CLI --sync, tests). Same code as the Celery path."""
    plan = begin(ctx, document_id, job_id)
    if plan is None:
        return DocumentStatus.FAILED
    for start, end in plan.batches:
        try:
            parse_batch(ctx, document_id, job_id, plan, start, end)
        except PermanentError as exc:
            fail(ctx, document_id, job_id, exc, Stage.PARSE)
            return DocumentStatus.FAILED
    return finalize(ctx, document_id, job_id, plan)


__all__ = [
    "POST_PARSE_STAGES",
    "ParsePlan",
    "PipelineContext",
    "begin",
    "default_context",
    "fail",
    "finalize",
    "parse_batch",
    "record_event",
    "run_document",
    "upsert_units",
]
