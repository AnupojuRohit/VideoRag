from celery import Celery

from app.core.config import get_settings

settings = get_settings()
celery_app = Celery(
    "videorag",
    broker=settings.redis_url,
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
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
