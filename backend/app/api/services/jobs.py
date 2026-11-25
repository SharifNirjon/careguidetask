"""Reprocessing: single document and bulk. Commit THEN enqueue: if the broker
is down, the job stays queued and the sweeper re-enqueues it."""

from __future__ import annotations

import logging
import uuid
from typing import Final

from sqlalchemy.orm import Session

from app.api.auth import Principal
from app.api.container import Container
from app.api.errors import Conflict, NotFound
from app.api.repositories.documents import DocumentRepository
from app.api.repositories.jobs import ActiveJobExists, JobRepository
from app.api.schemas.jobs import BulkReprocessItem, BulkReprocessRequest, BulkReprocessResponse, JobOut
from app.api.services import views
from app.models import ProcessingJob
from app.models.enums import ErrorCode
from app.workers.dispatch import JOB_TYPE_FOR_STAGE, FromStage

log = logging.getLogger(__name__)

# Retrying can't fix the file itself.
PERMANENT_ERRORS: Final = frozenset(
    {
        ErrorCode.CORRUPT_FILE,
        ErrorCode.PASSWORD_PROTECTED,
        ErrorCode.UNSUPPORTED_TYPE,
        ErrorCode.TYPE_MISMATCH,
        ErrorCode.EMPTY_FILE,
        ErrorCode.FILE_TOO_LARGE,
        ErrorCode.TOO_MANY_PAGES,
        ErrorCode.DECOMPRESSION_BOMB,
        ErrorCode.NO_TEXT_FOUND,
        ErrorCode.NOT_AN_INVOICE,
    }
)


class JobService:
    def __init__(self, session: Session, container: Container) -> None:
        self._session = session
        self._c = container
        self._jobs = JobRepository(session)
        self._docs = DocumentRepository(session)

    def get(self, job_id: uuid.UUID) -> JobOut:
        job = self._jobs.get(job_id)
        if job is None or self._docs.get(job.document_id) is None:
            raise NotFound(f"Job {job_id} was not found.", code="job_not_found")
        return views.job_out(job)

    def reprocess(
        self, document_id: uuid.UUID, stage: FromStage, force: bool, principal: Principal
    ) -> JobOut:
        document = self._docs.get(document_id)
        if document is None:
            raise NotFound(f"Document {document_id} was not found.", code="document_not_found")
        try:
            job = self._jobs.create(document, JOB_TYPE_FOR_STAGE[stage], principal.name)
        except ActiveJobExists as exc:
            self._session.rollback()
            raise Conflict("The document already has a queued or running job.", code="job_active") from exc
        self._session.commit()
        self._enqueue(job, stage, force)
        return views.job_out(job)

    def reprocess_failed(self, request: BulkReprocessRequest, principal: Principal) -> BulkReprocessResponse:
        limit = min(request.limit, self._c.api.bulk_reprocess_max)
        candidates = self._jobs.failed_documents(
            error_codes=request.error_codes, failed_since=request.failed_since, limit=limit
        )
        items: list[BulkReprocessItem] = []
        created: list[ProcessingJob] = []
        for candidate in candidates:
            if candidate.error_code in PERMANENT_ERRORS and not request.force:
                items.append(
                    BulkReprocessItem(
                        document_id=candidate.document_id,
                        outcome="skipped",
                        reason=f"permanent error {candidate.error_code}",
                    )
                )
                continue
            document = self._docs.get(candidate.document_id)
            if document is None:
                continue
            try:
                job = self._jobs.create(document, JOB_TYPE_FOR_STAGE[FromStage.PARSE], principal.name)
            except ActiveJobExists:
                items.append(
                    BulkReprocessItem(
                        document_id=candidate.document_id,
                        outcome="skipped",
                        reason="a job is already queued or running",
                    )
                )
                continue
            created.append(job)
            items.append(
                BulkReprocessItem(document_id=candidate.document_id, outcome="queued", job_id=job.id)
            )
        self._session.commit()
        for job in created:
            self._enqueue(job, FromStage.PARSE, force=False)
        queued = sum(i.outcome == "queued" for i in items)
        return BulkReprocessResponse(queued=queued, skipped=len(items) - queued, items=items)

    def _enqueue(self, job: ProcessingJob, stage: FromStage, force: bool) -> None:
        try:
            self._c.enqueuer.enqueue(job.document_id, job.id, stage, force=force)
        except Exception:
            log.warning(
                "enqueue failed; the sweeper will retry",
                extra={"document_id": str(job.document_id), "job_id": str(job.id)},
                exc_info=True,
            )
