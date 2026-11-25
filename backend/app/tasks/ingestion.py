import asyncio
import logging
from uuid import UUID

from app.celery_app import celery_app
from app.core.config import get_settings
from app.services.ingestion.pipeline import (
    RetryableJobError,
    process_ingestion_job,
    reconcile_processing_jobs as reconcile_jobs_async,
)

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.tasks.ingestion.process_video",
    max_retries=None,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_video(self, job_id: str) -> None:
    try:
        parsed_job_id = UUID(job_id)
    except ValueError as exc:
        logger.error("Celery received a malformed processing job identifier")
        raise ValueError("Invalid processing job identifier") from exc

    try:
        asyncio.run(
            process_ingestion_job(
                parsed_job_id,
                retry_count=self.request.retries,
            )
        )
    except RetryableJobError as exc:
        settings = get_settings()
        raise self.retry(
            exc=exc,
            countdown=exc.countdown,
            max_retries=settings.job_max_attempts,
        ) from exc


@celery_app.task(name="app.tasks.ingestion.reconcile_processing_jobs")
def reconcile_processing_jobs() -> int:
    return asyncio.run(reconcile_jobs_async())
