"""Celery wiring: task options and routing (no broker needed)."""

from app.pipeline.errors import TransientError
from app.workers import tasks
from app.workers.celery_app import QUEUE_DEFAULT, QUEUE_OCR, celery_app


def test_worker_settings() -> None:
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_soft_time_limit < conf.task_time_limit
    assert conf.task_default_queue == QUEUE_DEFAULT


def test_only_transient_errors_autoretry() -> None:
    for task in (tasks.process_document, tasks.parse_batch, tasks.finalize_document):
        assert TransientError in task.autoretry_for
        assert all(not issubclass(e, tasks.PermanentError) for e in task.autoretry_for)
        assert task.retry_backoff and task.retry_jitter and task.max_retries == 3


def test_ocr_queue_for_pdfs_and_images() -> None:
    from app.pipeline.orchestrator import ParsePlan
    from app.pipeline.types import FileKind

    assert ParsePlan(FileKind.PDF, [(0, 1)]).needs_ocr_queue
    assert ParsePlan(FileKind.TIFF, [(0, 1)]).needs_ocr_queue
    assert not ParsePlan(FileKind.XLSX, [(0, 1)]).needs_ocr_queue
    assert QUEUE_OCR != QUEUE_DEFAULT


def test_plan_round_trips_through_json() -> None:
    from app.pipeline.orchestrator import ParsePlan
    from app.pipeline.types import FileKind

    plan = ParsePlan(FileKind.PDF, [(0, 25), (25, 30)], ["repairing PDF document"])
    assert ParsePlan.from_dict(plan.to_dict()) == plan
