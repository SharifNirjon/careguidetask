"""processing_jobs and processing_events (the processing history)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, StrEnumText, created_at_col, enum_check, uuid_pk
from app.models.enums import ErrorCode, EventStatus, JobStatus, JobType, Stage

if TYPE_CHECKING:
    from app.models.document import Document


class ProcessingJob(Base):
    """One row per attempt to process a document."""

    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    job_type: Mapped[JobType] = mapped_column(StrEnumText(JobType))
    # Not unique: Celery retries reuse the same task id across attempts.
    celery_task_id: Mapped[str | None] = mapped_column(index=True)
    status: Mapped[JobStatus] = mapped_column(StrEnumText(JobStatus), server_default=JobStatus.QUEUED.value)
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    triggered_by: Mapped[str | None]
    error_code: Mapped[ErrorCode | None] = mapped_column(StrEnumText(ErrorCode))
    error_message: Mapped[str | None]
    queued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    document: Mapped[Document] = relationship(back_populates="jobs")
    events: Mapped[list[ProcessingEvent]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: ProcessingEvent.id,
    )

    __table_args__ = (
        UniqueConstraint("id", "document_id"),  # target for events' composite FK
        # At most one queued/running job per document: concurrent reprocess requests
        # can't both win (the API maps the violation to 409).
        Index(
            "uq_processing_jobs_document_id_active",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("finished_at >= started_at", name="run_window"),
        enum_check("job_type", JobType),
        enum_check("status", JobStatus),
        enum_check("error_code", ErrorCode),
    )


# (document_id, queued_at DESC) — leading document_id is also the FK index.
Index(
    "ix_processing_jobs_document_id_queued_at",
    ProcessingJob.document_id,
    ProcessingJob.queued_at.desc(),
)
Index("ix_processing_jobs_status", ProcessingJob.status)


class ProcessingEvent(Base):
    """Append-only per-stage log. UPDATE is blocked by a trigger.

    BIGINT identity rather than UUID: highest-volume table, insert-ordered,
    smaller indexes. document_id is denormalized for fast per-document history;
    a composite FK to processing_jobs(id, document_id) guarantees it matches
    the job's document.
    """

    __tablename__ = "processing_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    # Nullable: events can be logged before a job exists (e.g. ingest on upload).
    job_id: Mapped[uuid.UUID | None] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    stage: Mapped[Stage] = mapped_column(StrEnumText(Stage))
    status: Mapped[EventStatus] = mapped_column(StrEnumText(EventStatus))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()

    job: Mapped[ProcessingJob | None] = relationship(back_populates="events", foreign_keys=[job_id])

    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "document_id"],
            ["processing_jobs.id", "processing_jobs.document_id"],
            ondelete="CASCADE",
        ),
        # Leading document_id also serves as the document FK index.
        Index("ix_processing_events_document_id_created_at", "document_id", "created_at"),
        Index("ix_processing_events_job_id", "job_id"),
        CheckConstraint("duration_ms >= 0", name="duration_ms_nonneg"),
        enum_check("stage", Stage),
        enum_check("status", EventStatus),
    )
