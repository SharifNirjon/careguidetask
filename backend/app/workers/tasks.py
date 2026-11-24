"""Celery tasks: thin wrappers around app.pipeline.orchestrator.

Failure policy:
- TransientError / DB connectivity -> autoretry with exponential backoff + jitter
- PermanentError -> document failed immediately, task returns normally (no retry)
- anything else (a bug) or retries exhausted -> on_failure marks the document
  failed; the worker process itself keeps running
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from celery import Task, chord
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from app.config import get_pipeline_settings
from app.models import Stage
from app.models.enums import ErrorCode
from app.pipeline import orchestrator
from app.pipeline.errors import PermanentError, PipelineError, TransientError
from app.pipeline.orchestrator import ParsePlan
from app.workers.celery_app import QUEUE_DEFAULT, QUEUE_OCR, celery_app

log = logging.getLogger(__name__)
_settings = get_pipeline_settings()


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
    process_document.apply_async(args=[str(document_id), str(job_id)], queue=QUEUE_DEFAULT)


@celery_app.task(base=PipelineTask, bind=True, name="pipeline.process_document")
def process_document(self: Task, document_id: str, job_id: str) -> dict[str, Any]:
    ctx = orchestrator.default_context()
    doc_id, jid = uuid.UUID(document_id), uuid.UUID(job_id)
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
