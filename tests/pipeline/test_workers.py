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


def test_quota_deferral_reschedules_after_the_reset(monkeypatch) -> None:
    import uuid
    from datetime import UTC, datetime

    from app.pipeline import orchestrator
    from app.pipeline.errors import DeferredError

    resume = datetime(2026, 3, 2, tzinfo=UTC)
    scheduled: list[dict] = []

    def deferred(*_: object, **__: object) -> None:
        raise DeferredError(resume)

    monkeypatch.setattr(orchestrator, "default_context", lambda: None)
    monkeypatch.setattr(orchestrator, "job_finished", lambda *_: False)
    monkeypatch.setattr(orchestrator, "run_llm_stages", deferred)
    monkeypatch.setattr(tasks.extract_document, "apply_async", lambda **kw: scheduled.append(kw))
    result = tasks.extract_document.run(str(uuid.uuid4()), str(uuid.uuid4()))
    assert result["status"] == "deferred"
    assert scheduled[0]["queue"] == "llm" and scheduled[0]["eta"] >= resume


def test_tasks_skip_jobs_that_already_finished(monkeypatch) -> None:
    """A redelivered (or sweeper re-enqueued) message must not run a finished job again."""
    import uuid

    from app.pipeline import orchestrator

    def must_not_run(*_: object, **__: object) -> None:
        raise AssertionError("a finished job was run again")

    monkeypatch.setattr(orchestrator, "default_context", lambda: None)
    monkeypatch.setattr(orchestrator, "job_finished", lambda *_: True)
    for name in ("begin", "run_llm_stages", "run_validation_only"):
        monkeypatch.setattr(orchestrator, name, must_not_run)
    ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    for task in (tasks.process_document, tasks.extract_document, tasks.validate_document):
        assert task.run(*ids)["status"] == "skipped"
