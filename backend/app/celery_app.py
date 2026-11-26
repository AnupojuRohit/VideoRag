import logging

from celery import Celery
from celery.signals import setup_logging, worker_process_init

from app.core.config import get_settings
from app.core.logging_config import configure_logging

settings = get_settings()
celery_app = Celery(
    "videorag",
    broker=settings.redis_url,
)


def _configure_worker_logging(loglevel: int | str | None = None, **_: object) -> None:
    if isinstance(loglevel, int):
        configured_level = logging.getLevelName(loglevel)
    else:
        configured_level = loglevel
    configure_logging(
        configured_level
        if isinstance(configured_level, str)
        else settings.log_level
    )


def _configure_forked_worker_logging(**_: object) -> None:
    inherited_level = logging.getLevelName(logging.getLogger().level)
    _configure_worker_logging(
        inherited_level if isinstance(inherited_level, str) else settings.log_level
    )


setup_logging.connect(_configure_worker_logging, weak=False)
worker_process_init.connect(_configure_forked_worker_logging, weak=False)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_hijack_root_logger=False,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    result_backend=None,
    task_publish_retry=False,
    broker_connection_timeout=2,
    broker_transport_options={
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        "retry_on_timeout": False,
    },
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    imports=("app.tasks.ingestion",),
    beat_schedule={
        "reconcile-processing-jobs": {
            "task": "app.tasks.ingestion.reconcile_processing_jobs",
            "schedule": settings.job_reconcile_interval_seconds,
        },
    },
)
