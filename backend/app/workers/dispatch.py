"""Which task runs a job, and how it's enqueued (the only place that knows).

Every enqueue carries the caller's request id as a Celery header; the worker
binds it to its logs (see celery_app), so one id traces a request end to end.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Final, Protocol

from app.logging_setup import current_request_id
from app.models.enums import JobType


class FromStage(StrEnum):
    PARSE = "parse"  # the whole pipeline again
    EXTRACT = "extract"  # keep units/segments, re-run the LLM + validation
    VALIDATE = "validate"  # re-run validation only


JOB_TYPE_FOR_STAGE: Final = {
    FromStage.PARSE: JobType.REPROCESS,
    FromStage.EXTRACT: JobType.REEXTRACT,
    FromStage.VALIDATE: JobType.REVALIDATE,
}
STAGE_FOR_JOB_TYPE: Final = {
    JobType.FULL: FromStage.PARSE,
    JobType.REPROCESS: FromStage.PARSE,
    JobType.REEXTRACT: FromStage.EXTRACT,
    JobType.REVALIDATE: FromStage.VALIDATE,
}
REQUEST_ID_HEADER: Final = "request_id"


class JobEnqueuer(Protocol):
    def enqueue(
        self, document_id: uuid.UUID, job_id: uuid.UUID, stage: FromStage, *, force: bool = False
    ) -> None:
        """Called only AFTER the job row is committed."""
        ...

    def enqueue_reconciliation(self, run_id: uuid.UUID) -> None:
        """Called only AFTER the reconciliation_runs row is committed."""
        ...

    def enqueue_embedding(self, document_id: uuid.UUID) -> None:
        """Embed the document's pending chunks (off the critical path; best effort)."""
        ...


class CeleryEnqueuer:
    def enqueue_embedding(self, document_id: uuid.UUID) -> None:
        from app.workers import tasks

        tasks.embed_pending.apply_async(
            kwargs={"document_id": str(document_id)},
            queue=tasks.QUEUE_LLM,
            headers={REQUEST_ID_HEADER: current_request_id()},
        )

    def enqueue_reconciliation(self, run_id: uuid.UUID) -> None:
        from app.workers import tasks

        tasks.run_reconciliation.apply_async(
            args=[str(run_id)], queue=tasks.QUEUE_DEFAULT, headers={REQUEST_ID_HEADER: current_request_id()}
        )

    def enqueue(
        self, document_id: uuid.UUID, job_id: uuid.UUID, stage: FromStage, *, force: bool = False
    ) -> None:
        from app.workers import tasks  # imported lazily: the API process needn't load Celery tasks at import

        headers = {REQUEST_ID_HEADER: current_request_id()}
        args = [str(document_id), str(job_id)]
        if stage is FromStage.PARSE:
            tasks.process_document.apply_async(args=args, queue=tasks.QUEUE_DEFAULT, headers=headers)
        elif stage is FromStage.EXTRACT:
            tasks.extract_document.apply_async(
                args=args, kwargs={"force": force}, queue=tasks.QUEUE_LLM, headers=headers
            )
        else:
            tasks.validate_document.apply_async(args=args, queue=tasks.QUEUE_DEFAULT, headers=headers)
