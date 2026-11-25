"""SQL for processing_jobs: creation (guarded by the one-active-job index)
and bulk-reprocess candidates."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Document, ProcessingJob
from app.models.enums import DocumentStatus, ErrorCode, JobType

ACTIVE_JOB_INDEX: Final = "uq_processing_jobs_document_id_active"


class ActiveJobExists(Exception):
    """The document already has a queued or running job (the partial unique index said so)."""


@dataclass(frozen=True, slots=True)
class FailedDocument:
    document_id: uuid.UUID
    error_code: ErrorCode | None


def is_active_job_violation(exc: IntegrityError) -> bool:
    return ACTIVE_JOB_INDEX in str(exc.orig)


class JobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, job_id: uuid.UUID) -> ProcessingJob | None:
        return self._session.get(ProcessingJob, job_id)

    def create(self, document: Document, job_type: JobType, triggered_by: str) -> ProcessingJob:
        """Insert in a SAVEPOINT so a violation leaves the outer transaction usable
        (bulk reprocess keeps going after one document is busy)."""
        job = ProcessingJob(document_id=document.id, job_type=job_type, triggered_by=triggered_by)
        try:
            with self._session.begin_nested():
                self._session.add(job)
                self._session.flush()
        except IntegrityError as exc:
            if is_active_job_violation(exc):
                raise ActiveJobExists from exc
            raise
        document.status = DocumentStatus.QUEUED
        document.error_code = document.error_message = None
        return job

    def failed_documents(
        self, *, error_codes: Collection[ErrorCode] | None, failed_since: datetime | None, limit: int
    ) -> list[FailedDocument]:
        query = select(Document.id, Document.error_code).where(
            Document.status == DocumentStatus.FAILED, Document.deleted_at.is_(None)
        )
        if error_codes:
            query = query.where(Document.error_code.in_(list(error_codes)))
        if failed_since is not None:
            query = query.where(Document.processing_finished_at >= failed_since)
        rows = self._session.execute(
            query.order_by(Document.processing_finished_at.desc().nulls_last(), Document.id).limit(limit)
        ).all()
        return [FailedDocument(document_id, code) for document_id, code in rows]
