from celery import Celery

from app.core.config import get_settings

settings = get_settings()
celery_app = Celery(
    "videorag",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)
