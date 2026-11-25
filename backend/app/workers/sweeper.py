"""Stale-job sweeper (Celery beat). Idempotent: running it twice changes nothing more.

- queued longer than STALE_QUEUED_MINUTES -> enqueue again (the API commits
  THEN enqueues; if the broker was down, the job is only a row). Quota-deferred
  jobs are skipped: they already have an ETA task waiting for the reset.
- running past JOB_MAX_RUNTIME_MINUTES -> failed with WORKER_TIMEOUT (the worker
  died without reporting), which makes the document eligible for reprocessing.

Rows are locked FOR UPDATE SKIP LOCKED, so two beat instances never handle the
same job; enqueues happen only after the commit.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Document, ProcessingEvent, ProcessingJob
from app.models.enums import DocumentStatus, ErrorCode, EventStatus, JobStatus, Stage
from app.pipeline.errors import USER_MESSAGES
from app.workers.dispatch import STAGE_FOR_JOB_TYPE, FromStage, JobEnqueuer

log = logging.getLogger(__name__)

NOT_REQUEUED: Final = (ErrorCode.LLM_QUOTA_DEFERRED,)


@dataclass(frozen=True, slots=True)
class SweepReport:
    requeued: list[uuid.UUID] = field(default_factory=list)
    timed_out: list[uuid.UUID] = field(default_factory=list)


class Sweeper:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        enqueuer: JobEnqueuer,
        stale_queued: timedelta,
        max_runtime: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._enqueuer = enqueuer
        self._stale_queued = stale_queued
        self._max_runtime = max_runtime
        self._now = now

    def run(self) -> SweepReport:
        report = SweepReport(timed_out=self._time_out_overdue(), requeued=[])
        report.requeued.extend(self._requeue_stale())
        if report.requeued or report.timed_out:
            log.info("sweep", extra={"requeued": len(report.requeued), "timed_out": len(report.timed_out)})
        return report

    def _requeue_stale(self) -> list[uuid.UUID]:
        now = self._now()
        to_enqueue: list[tuple[uuid.UUID, uuid.UUID, FromStage]] = []
        with self._session_factory() as session, session.begin():
            jobs = session.scalars(
                select(ProcessingJob)
                .join(Document, Document.id == ProcessingJob.document_id)
                .where(
                    ProcessingJob.status == JobStatus.QUEUED,
                    ProcessingJob.queued_at < now - self._stale_queued,
                    ProcessingJob.error_code.is_(None) | ProcessingJob.error_code.not_in(NOT_REQUEUED),
                    Document.deleted_at.is_(None),
                )
                .order_by(ProcessingJob.queued_at)
                .with_for_update(of=ProcessingJob, skip_locked=True)
            ).all()
            for job in jobs:
                stage = STAGE_FOR_JOB_TYPE.get(job.job_type)
                if stage is None:
                    continue
                job.queued_at = now  # the next sweep waits a full interval again
                to_enqueue.append((job.document_id, job.id, stage))
        requeued = []
        for document_id, job_id, stage in to_enqueue:
            try:
                self._enqueuer.enqueue(document_id, job_id, stage)
                requeued.append(job_id)
            except Exception:
                log.warning("re-enqueue failed", extra={"job_id": str(job_id)}, exc_info=True)
        return requeued

    def _time_out_overdue(self) -> list[uuid.UUID]:
        now = self._now()
        message = USER_MESSAGES[ErrorCode.WORKER_TIMEOUT]
        with self._session_factory() as session, session.begin():
            jobs = session.scalars(
                select(ProcessingJob)
                .where(
                    ProcessingJob.status == JobStatus.RUNNING,
                    ProcessingJob.started_at < now - self._max_runtime,
                )
                .order_by(ProcessingJob.started_at)
                .with_for_update(skip_locked=True)
            ).all()
            for job in jobs:
                job.status, job.error_code, job.error_message, job.finished_at = (
                    JobStatus.FAILED,
                    ErrorCode.WORKER_TIMEOUT,
                    message,
                    now,
                )
                document = session.get_one(Document, job.document_id)
                document.status, document.error_code, document.error_message = (
                    DocumentStatus.FAILED,
                    ErrorCode.WORKER_TIMEOUT,
                    message,
                )
                document.processing_finished_at = now
                session.add(
                    ProcessingEvent(
                        job_id=job.id,
                        document_id=job.document_id,
                        stage=Stage.PARSE,
                        status=EventStatus.FAILED,
                        details={"code": ErrorCode.WORKER_TIMEOUT.value, "message": message, "by": "sweeper"},
                    )
                )
            return [job.id for job in jobs]
