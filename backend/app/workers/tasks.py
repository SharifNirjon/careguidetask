"""Celery tasks: thin wrappers around app.pipeline.orchestrator.

Failure policy:
- TransientError / DB connectivity -> autoretry with exponential backoff + jitter
- PermanentError -> document failed immediately, task returns normally (no retry)
- anything else (a bug) or retries exhausted -> on_failure marks the document
  failed; the worker process itself keeps running
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import timedelta
from typing import Any

from celery import Task, chord
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from app.config import (
    get_api_settings,
    get_llm_settings,
    get_pipeline_settings,
    get_search_settings,
    get_validation_settings,
)
from app.db.session import get_sessionmaker
from app.logging_setup import current_request_id
from app.models import DocumentStatus, Stage
from app.models.enums import ErrorCode
from app.pipeline import orchestrator
from app.pipeline.errors import DeferredError, PermanentError, PipelineError, TransientError
from app.pipeline.orchestrator import ParsePlan
from app.validation.reconciliation import ReconciliationService
from app.workers.celery_app import QUEUE_DEFAULT, QUEUE_LLM, QUEUE_OCR, celery_app
from app.workers.dispatch import REQUEST_ID_HEADER, CeleryEnqueuer, FromStage
from app.workers.sweeper import Sweeper

log = logging.getLogger(__name__)
_settings = get_pipeline_settings()
_rng = random.Random()
SKIPPED_FINISHED = {"status": "skipped", "reason": "job already finished"}


class PipelineTask(Task):
    autoretry_for = (TransientError, OperationalError)
    retry_backoff = True
    retry_backoff_max = _settings.task_retry_backoff_max_s
    retry_jitter = True
    max_retries = _settings.task_max_retries
    acks_late = True

    def on_failure(self, exc: BaseException, task_id: str, args: tuple, kwargs: dict, einfo: Any) -> None:
        document_id, job_id = _ids(args, kwargs)
        if document_id is None:
            return
        error = (
            exc
            if isinstance(exc, PipelineError)
            else PermanentError(
                ErrorCode.TIMEOUT if isinstance(exc, SoftTimeLimitExceeded) else ErrorCode.INTERNAL_ERROR,
                details={"exception": type(exc).__name__, "task_id": task_id},
            )
        )
        try:
            orchestrator.fail(orchestrator.default_context(), document_id, job_id, error, Stage.PARSE)
        except Exception:  # the DB may be the thing that is down; never raise from a hook
            log.exception(
                "could not record task failure",
                extra={"document_id": str(document_id), "job_id": str(job_id)},
            )


def _ids(args: tuple, kwargs: dict) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    document_id = kwargs.get("document_id", args[0] if args else None)
    job_id = kwargs.get("job_id", args[1] if len(args) > 1 else None)
    if document_id is None or job_id is None:
        return None, None
    return uuid.UUID(str(document_id)), uuid.UUID(str(job_id))


def enqueue_document(document_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """Intake hook: called only AFTER the document/job rows are committed."""
    CeleryEnqueuer().enqueue(document_id, job_id, FromStage.PARSE)


@celery_app.task(base=PipelineTask, bind=True, name="pipeline.process_document")
def process_document(self: Task, document_id: str, job_id: str) -> dict[str, Any]:
    ctx = orchestrator.default_context()
    doc_id, jid = uuid.UUID(document_id), uuid.UUID(job_id)
    if orchestrator.job_finished(ctx, jid):
        return SKIPPED_FINISHED
    plan = orchestrator.begin(ctx, doc_id, jid, attempt=self.request.retries + 1)
    if plan is None:
        return {"status": "failed"}
    queue = QUEUE_OCR if plan.needs_ocr_queue else QUEUE_DEFAULT
    header = [
        parse_batch.si(document_id, job_id, plan.to_dict(), start, end).set(queue=queue)
        for start, end in plan.batches
    ]
    callback = finalize_document.si(document_id, job_id, plan.to_dict()).set(queue=QUEUE_DEFAULT)
    chord(header)(callback.on_error(chord_failed.s(document_id, job_id)))
    return {"status": "dispatched", "batches": len(header)}


@celery_app.task(base=PipelineTask, bind=True, name="pipeline.parse_batch")
def parse_batch(
    self: Task, document_id: str, job_id: str, plan: dict[str, Any], start: int, end: int
) -> dict[str, Any]:
    ctx = orchestrator.default_context()
    try:
        return orchestrator.parse_batch(
            ctx, uuid.UUID(document_id), uuid.UUID(job_id), ParsePlan.from_dict(plan), start, end
        )
    except PermanentError as exc:
        orchestrator.fail(ctx, uuid.UUID(document_id), uuid.UUID(job_id), exc, Stage.PARSE)
        return {"status": "failed", "error_code": exc.code.value}


@celery_app.task(base=PipelineTask, bind=True, name="pipeline.finalize_document")
def finalize_document(self: Task, document_id: str, job_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    status = orchestrator.finalize(
        orchestrator.default_context(), uuid.UUID(document_id), uuid.UUID(job_id), ParsePlan.from_dict(plan)
    )
    if status is DocumentStatus.PROCESSING:  # parsed: hand over to the LLM queue
        extract_document.apply_async(args=[document_id, job_id], queue=QUEUE_LLM)
    return {"status": status.value}


@celery_app.task(base=PipelineTask, bind=True, name="pipeline.extract_document")
def extract_document(self: Task, document_id: str, job_id: str, force: bool = False) -> dict[str, Any]:
    """extract -> validate -> embed. Quota exhaustion re-schedules this task for
    after the UTC reset (a NEW task, so deferrals don't consume retries)."""
    if orchestrator.job_finished(orchestrator.default_context(), uuid.UUID(job_id)):
        return SKIPPED_FINISHED
    try:
        status = orchestrator.run_llm_stages(
            orchestrator.default_context(), uuid.UUID(document_id), uuid.UUID(job_id), force=force
        )
    except DeferredError as deferred:
        eta = deferred.resume_at + timedelta(seconds=_rng.uniform(0, _settings.llm_defer_jitter_s))
        extract_document.apply_async(
            args=[document_id, job_id],
            kwargs={"force": force},
            queue=QUEUE_LLM,
            eta=eta,
            headers={REQUEST_ID_HEADER: current_request_id()},
        )
        return {"status": "deferred", "eta": eta.isoformat()}
    return {"status": status.value}


@celery_app.task(base=PipelineTask, bind=True, name="pipeline.validate_document")
def validate_document(self: Task, document_id: str, job_id: str) -> dict[str, Any]:
    """Re-run validation only (rules or settings changed, or a reviewer asked)."""
    if orchestrator.job_finished(orchestrator.default_context(), uuid.UUID(job_id)):
        return SKIPPED_FINISHED
    status = orchestrator.run_validation_only(
        orchestrator.default_context(), uuid.UUID(document_id), uuid.UUID(job_id)
    )
    return {"status": status.value}


@celery_app.task(name="pipeline.chord_failed")
def chord_failed(*args: Any) -> None:
    """Errback for the parse chord: a batch failed for good, so the callback never ran.

    Celery calls errbacks as (request, exc, traceback, *partial_args); the last
    two positional args are always (document_id, job_id).
    """
    document_id, job_id = args[-2], args[-1]
    exc = next((a for a in args if isinstance(a, BaseException)), None)
    error = (
        exc
        if isinstance(exc, PipelineError)
        else PermanentError(
            ErrorCode.INTERNAL_ERROR, details={"exception": type(exc).__name__ if exc else "unknown"}
        )
    )
    orchestrator.fail(
        orchestrator.default_context(),
        uuid.UUID(str(document_id)),
        uuid.UUID(str(job_id)),
        error,
        Stage.PARSE,
    )


@celery_app.task(name="reconciliation.run", acks_late=True)
def run_reconciliation(run_id: str) -> dict[str, Any]:
    """Execute a batch reconciliation created by the API (POST /reconciliations)."""
    service = ReconciliationService(session_factory=get_sessionmaker(), settings=get_validation_settings())
    report = service.execute(uuid.UUID(run_id))
    return {"run_id": run_id, **report.summary()["counts"]}


@celery_app.task(name="ops.sweep")
def sweep() -> dict[str, int]:
    """Celery beat: re-enqueue stale queued jobs, fail overdue running ones."""
    api = get_api_settings()
    report = Sweeper(
        session_factory=get_sessionmaker(),
        enqueuer=CeleryEnqueuer(),
        stale_queued=timedelta(minutes=api.stale_queued_minutes),
        max_runtime=timedelta(minutes=api.job_max_runtime_minutes),
    ).run()
    return {"requeued": len(report.requeued), "timed_out": len(report.timed_out)}


def _search_runtime() -> Any:
    from redis import Redis

    from app.search.runtime import build_runtime

    llm = get_llm_settings()
    return build_runtime(llm, get_search_settings(), Redis.from_url(llm.redis_url))


@celery_app.task(name="search.embed_pending", acks_late=True)
def embed_pending(document_id: str | None = None) -> dict[str, Any]:
    """Embed pending chunks (one document after processing, or the whole queue
    from the beat sweep). Out of quota: the chunks simply stay pending, keyword
    search keeps working, and the next sweep after the UTC reset resumes. Never
    fails a document; never retried in a loop."""
    search = get_search_settings()
    embedder = _search_runtime().chunk_embedder(get_sessionmaker(), get_llm_settings().embed_batch_size)
    report = embedder.embed_pending(
        document_id=uuid.UUID(document_id) if document_id else None, limit=search.embed_pending_limit
    )
    if report.deferred or report.error:
        log.info(
            "embedding deferred",
            extra={
                "document_id": document_id,
                "embedded": report.embedded,
                "deferred_until": report.deferred_until.isoformat() if report.deferred_until else None,
                "error": report.error,
            },
        )
    return {
        "embedded": report.embedded,
        "requests": report.requests,
        "deferred": report.deferred,
        "error": report.error,
    }


@celery_app.task(name="search.reembed", acks_late=True)
def reembed(from_space: str | None = None, limit: int | None = None) -> dict[str, Any]:
    """Move fallback-space chunks to the primary space (low priority, beat)."""
    llm, search = get_llm_settings(), get_search_settings()
    spaces = [from_space] if from_space else [m.space for m in llm.embedding_models[1:]]
    report = (
        _search_runtime()
        .reembedder(get_sessionmaker(), llm.embed_batch_size)
        .run(from_spaces=spaces, limit=limit or search.reembed_batch_limit)
    )
    return {"selected": report.selected, "moved": report.moved, "skipped": report.skipped_reason}
