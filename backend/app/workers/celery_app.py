"""Celery application.

    celery -A app.workers.celery_app worker -Q ocr,default --concurrency=<cpus>

Queues: `ocr` (CPU-heavy page batches: PDFs and images) and `default`
(orchestration, spreadsheets/CSV, finalize). Run them as separate worker pools
in production so a burst of scans cannot starve spreadsheet processing.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from app.config import get_pipeline_settings
from app.db.session import dispose_engine_after_fork
from app.logging_setup import configure_logging

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
)


@worker_process_init.connect
def _init_worker_process(**_: object) -> None:
    configure_logging()
    dispose_engine_after_fork()  # never share pooled DB sockets across forks
