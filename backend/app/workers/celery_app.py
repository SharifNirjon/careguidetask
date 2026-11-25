"""Celery application.

    celery -A app.workers.celery_app worker -Q ocr,default --concurrency=<cpus>

Queues: `ocr` (CPU-heavy page batches: PDFs and images) and `default`
(orchestration, spreadsheets/CSV, finalize). Run them as separate worker pools
in production so a burst of scans cannot starve spreadsheet processing.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import task_postrun, task_prerun, worker_process_init

from app.config import get_api_settings, get_pipeline_settings, get_search_settings
from app.db.session import dispose_engine_after_fork
from app.logging_setup import bind_request_id, configure_logging

QUEUE_DEFAULT = "default"
QUEUE_OCR = "ocr"
QUEUE_LLM = "llm"  # rate-limited LLM work: its waits must never block OCR workers

_settings = get_pipeline_settings()

celery_app = Celery(
    "careguide", broker=_settings.redis_url, backend=_settings.redis_url, include=["app.workers.tasks"]
)
celery_app.conf.update(
    task_default_queue=QUEUE_DEFAULT,
    task_acks_late=True,  # a crashed worker's task is redelivered, not lost
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long tasks: don't hoard work on one worker
    task_soft_time_limit=_settings.task_soft_time_limit_s,
    task_time_limit=_settings.task_hard_time_limit_s,
    worker_max_tasks_per_child=_settings.worker_max_tasks_per_child,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=24 * 3600,
    broker_connection_retry_on_startup=True,
    # Quota deferrals are scheduled ~24 h ahead (eta). With acks_late, Redis
    # redelivers any task not acked within the visibility timeout, so it must
    # exceed the longest eta. Trade-off: a task on a crashed HOST waits this long.
    broker_transport_options={"visibility_timeout": _settings.broker_visibility_timeout_s},
    # `celery -A app.workers.celery_app beat`: run exactly ONE beat process.
    beat_schedule={
        "sweep-stale-jobs": {"task": "ops.sweep", "schedule": float(get_api_settings().sweeper_interval_s)},
        # Embedding runs on the rate-limited llm queue, after extraction work.
        "embed-pending-chunks": {
            "task": "search.embed_pending",
            "schedule": float(get_search_settings().embed_sweep_interval_s),
            "options": {"queue": QUEUE_LLM, "priority": 9},
        },
        "reembed-fallback-space": {
            "task": "search.reembed",
            "schedule": float(get_search_settings().reembed_interval_s),
            "options": {"queue": QUEUE_LLM, "priority": 9},
        },
    },
)


@worker_process_init.connect
def _init_worker_process(**_: object) -> None:
    configure_logging()
    dispose_engine_after_fork()  # never share pooled DB sockets across forks


@task_prerun.connect
def _bind_request_id(task: object = None, **_: object) -> None:
    """Restore the API request id sent as a task header, so worker logs carry it."""
    request = getattr(task, "request", None)
    bind_request_id(getattr(request, "request_id", None))


@task_postrun.connect
def _clear_request_id(**_: object) -> None:
    bind_request_id(None)
