"""Job progress from processing_events (pure).

A stage counts as done when it wrote a terminal, document-scope event
(per-batch and per-segment events are partial progress, not completion).
Parse progress within the stage comes from the batch events' unit counts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from app.api.schemas.documents import Progress
from app.models.enums import EventStatus, JobStatus, JobType, Stage

MS_PER_S: Final = 1000
PERCENT: Final = 100
PARTIAL_SCOPES: Final = frozenset({"batch", "segment"})
FINISHED_JOB: Final = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})
STAGES_FOR_JOB: Final[dict[JobType, tuple[Stage, ...]]] = {
    JobType.FULL: (Stage.PARSE, Stage.SEGMENT, Stage.EXTRACT, Stage.VALIDATE, Stage.EMBED),
    JobType.REPROCESS: (Stage.PARSE, Stage.SEGMENT, Stage.EXTRACT, Stage.VALIDATE, Stage.EMBED),
    JobType.REEXTRACT: (Stage.EXTRACT, Stage.VALIDATE, Stage.EMBED),
    JobType.REVALIDATE: (Stage.VALIDATE,),
    JobType.REEMBED: (Stage.EMBED,),
}


@dataclass(frozen=True, slots=True)
class JobView:
    job_type: JobType
    status: JobStatus
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class EventView:
    stage: Stage
    status: EventStatus
    details: dict[str, Any]


def compute_progress(
    job: JobView | None, events: Sequence[EventView], *, units_total: int | None, now: datetime
) -> Progress:
    if job is None:
        return Progress(
            stage=None,
            stages_done=0,
            stages_total=0,
            units_done=0,
            units_total=units_total,
            percent=0,
            started_at=None,
            elapsed_ms=None,
        )
    stages = STAGES_FOR_JOB[job.job_type]
    done = {
        e.stage
        for e in events
        if e.status is not EventStatus.STARTED and e.details.get("scope") not in PARTIAL_SCOPES
    }
    stages_done = sum(stage in done for stage in stages)
    units_done = sum(
        int(e.details.get("units", 0))
        for e in events
        if e.stage is Stage.PARSE and e.details.get("scope") == "batch"
    )
    finished = job.status in FINISHED_JOB
    current = None if finished else next((s for s in stages if s not in done), None)

    if job.status is JobStatus.SUCCEEDED:
        percent = PERCENT
    else:
        partial = 0.0
        if current is Stage.PARSE and units_total:
            partial = min(units_done / units_total, 1.0)
        percent = min(int(PERCENT * (stages_done + partial) / len(stages)), PERCENT - 1)

    end = job.finished_at if finished and job.finished_at else now
    elapsed = int((end - job.started_at).total_seconds() * MS_PER_S) if job.started_at else None
    return Progress(
        stage=current,
        stages_done=stages_done,
        stages_total=len(stages),
        units_done=units_done,
        units_total=units_total,
        percent=percent,
        started_at=job.started_at,
        elapsed_ms=elapsed,
    )
