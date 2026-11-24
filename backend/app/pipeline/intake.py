"""File intake: store, fingerprint, detect type, register, enqueue.

Used by the CLI now and by the upload API later. Rejections that can be decided
at intake (empty, too large, wrong/unsupported type, zip bomb, macros) raise
PermanentError and leave nothing behind: no blob, no row. The caller turns them
into an immediate 4xx. Everything else becomes a queued document.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PipelineSettings, get_pipeline_settings
from app.models import (
    Document,
    DocumentStatus,
    EventStatus,
    JobType,
    ProcessingEvent,
    ProcessingJob,
    Stage,
)
from app.models.enums import ErrorCode
from app.pipeline import filetypes
from app.pipeline.errors import PermanentError
from app.pipeline.storage import Storage
from app.pipeline.text_utils import clean_text

log = logging.getLogger(__name__)

MAX_FILENAME_CHARS = 255

Enqueue = Callable[[uuid.UUID, uuid.UUID], None]


class _HashingLimitedReader:
    """Wraps an upload stream: hashes and counts bytes, aborts past the limit."""

    def __init__(self, stream: BinaryIO, limit_bytes: int) -> None:
        self._stream = stream
        self._limit = limit_bytes
        self._sha = hashlib.sha256()
        self.size = 0

    def read(self, n: int = -1) -> bytes:
        chunk = self._stream.read(n)
        self.size += len(chunk)
        if self.size > self._limit:
            raise PermanentError(ErrorCode.FILE_TOO_LARGE, details={"limit_bytes": self._limit})
        self._sha.update(chunk)
        return chunk

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()


def ingest_file(
    stream: BinaryIO,
    original_filename: str,
    uploaded_by: str | None,
    session: Session,
    *,
    storage: Storage,
    enqueue: Enqueue,
    settings: PipelineSettings | None = None,
    job_type: JobType = JobType.FULL,
) -> Document:
    """Store an upload and register it for processing.

    The documents/processing_jobs rows are COMMITTED before `enqueue` is called,
    so a worker can never pick up a job whose rows don't exist yet.
    Duplicate content (same sha256) is stored and linked, never rejected.
    """
    settings = settings or get_pipeline_settings()
    started = time.perf_counter()
    filename = clean_text(original_filename, MAX_FILENAME_CHARS).text or "unnamed"
    extension = filetypes.normalize_extension(filename)

    reader = _HashingLimitedReader(stream, settings.max_file_size_bytes)
    key = storage.put(reader)  # type: ignore[arg-type]  # duck-typed .read()
    try:
        if reader.size == 0:
            raise PermanentError(ErrorCode.EMPTY_FILE)
        with storage.local_path(key) as path:
            detection = filetypes.detect(path, extension, settings)
    except BaseException:
        storage.delete(key)
        raise

    try:
        document = Document(
            original_filename=filename,
            storage_key=key,
            mime_type=detection.kind.mime_type,
            file_ext=extension or None,
            size_bytes=reader.size,
            sha256=reader.sha256,
            status=DocumentStatus.QUEUED,
            uploaded_by=uploaded_by,
            duplicate_of_document_id=_find_original(session, reader.sha256),
        )
        session.add(document)
        session.flush()
        job = ProcessingJob(document_id=document.id, job_type=job_type, triggered_by=uploaded_by)
        session.add(job)
        session.flush()
        session.add(
            ProcessingEvent(
                job_id=job.id,
                document_id=document.id,
                stage=Stage.INGEST,
                status=EventStatus.SUCCEEDED,
                duration_ms=int((time.perf_counter() - started) * 1000),
                details={
                    "detected_type": detection.kind.value,
                    "size_bytes": reader.size,
                    "duplicate": document.duplicate_of_document_id is not None,
                    **detection.details,
                },
            )
        )
        session.commit()
    except BaseException:
        session.rollback()
        storage.delete(key)
        raise

    log.info(
        "document ingested",
        extra={
            "document_id": str(document.id),
            "job_id": str(job.id),
            "stage": "ingest",
            "detected_type": detection.kind.value,
            "size_bytes": reader.size,
        },
    )
    enqueue(document.id, job.id)
    return document


def _find_original(session: Session, sha256: str) -> uuid.UUID | None:
    """Earliest live document with the same bytes (the 'original' of a duplicate)."""
    return session.scalar(
        select(Document.id)
        .where(Document.sha256 == sha256, Document.deleted_at.is_(None))
        .order_by(Document.created_at, Document.id)
        .limit(1)
    )
