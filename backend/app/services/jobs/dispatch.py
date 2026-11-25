import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from kombu.exceptions import OperationalError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.privacy import EXTERNAL_PROCESSING_CONSENT_VERSION
from app.db.session import SessionLocal
from app.models.enums import LifecycleStatus
from app.models.processing_job import ProcessingJob
from app.models.video_asset import VideoAsset
from app.celery_app import celery_app

logger = logging.getLogger(__name__)
TASK_NAME = "app.tasks.ingestion.process_video"


async def schedule_job(job_id: UUID) -> LifecycleStatus | None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.id == job_id)
                .with_for_update()
            )
            if job is None:
                return None
            asset = await session.get(VideoAsset, job.video_id)
            if asset is None:
                logger.error(
                    "Processing job has no associated asset",
                    extra={"job_id": str(job_id)},
                )
                return None
            if (
                not asset.external_processing_consent
                or asset.external_processing_consent_version
                != EXTERNAL_PROCESSING_CONSENT_VERSION
            ):
                return job.status

            can_schedule = job.status in {
                LifecycleStatus.UPLOADED,
                LifecycleStatus.QUEUED,
            } or (
                job.status == LifecycleStatus.PROCESSING
                and job.lease_expires_at is not None
                and job.lease_expires_at <= now
            )
            if not can_schedule:
                return job.status
            if (
                job.last_enqueued_at is not None
                and job.last_enqueued_at
                > now - timedelta(seconds=settings.job_dispatch_cooldown_seconds)
            ):
                return job.status

            job.status = LifecycleStatus.QUEUED
            asset.status = LifecycleStatus.QUEUED
            job.last_enqueued_at = now
            await session.flush()

    try:
        celery_app.send_task(TASK_NAME, args=[str(job_id)])
    except (OperationalError, OSError, TimeoutError) as exc:
        async with SessionLocal() as session:
            async with session.begin():
                job = await session.scalar(
                    select(ProcessingJob)
                    .where(ProcessingJob.id == job_id)
                    .with_for_update()
                )
                if job is not None and job.status == LifecycleStatus.QUEUED:
                    job.last_enqueued_at = None
        logger.warning(
            "Unable to publish processing job; PostgreSQL reconciliation will retry",
            extra={"job_id": str(job_id), "error_type": type(exc).__name__},
        )
    return LifecycleStatus.QUEUED


async def reconcile_jobs(batch_size: int = 100) -> int:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.job_dispatch_cooldown_seconds)
    async with SessionLocal() as session:
        async with session.begin():
            jobs = list(
                (
                    await session.scalars(
                        select(ProcessingJob)
                        .join(VideoAsset, VideoAsset.id == ProcessingJob.video_id)
                        .where(
                            VideoAsset.external_processing_consent.is_(True),
                            (
                                VideoAsset.external_processing_consent_version
                                == EXTERNAL_PROCESSING_CONSENT_VERSION
                            ),
                            (
                                (
                                    ProcessingJob.status.in_(
                                        [
                                            LifecycleStatus.UPLOADED,
                                            LifecycleStatus.QUEUED,
                                        ]
                                    )
                                    & (
                                        ProcessingJob.last_enqueued_at.is_(None)
                                        | (ProcessingJob.last_enqueued_at <= cutoff)
                                    )
                                )
                                | (
                                    (ProcessingJob.status == LifecycleStatus.PROCESSING)
                                    & (ProcessingJob.lease_expires_at <= now)
                                    & (
                                        ProcessingJob.last_enqueued_at.is_(None)
                                        | (ProcessingJob.last_enqueued_at <= cutoff)
                                    )
                                )
                            ),
                        )
                        .order_by(ProcessingJob.created_at)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for job in jobs:
                job.last_enqueued_at = now
                if job.status == LifecycleStatus.UPLOADED:
                    job.status = LifecycleStatus.QUEUED
                asset = await session.get(VideoAsset, job.video_id)
                if asset is not None:
                    asset.status = LifecycleStatus.QUEUED
            job_ids = [job.id for job in jobs]

    dispatched = 0
    for job_id in job_ids:
        try:
            celery_app.send_task(TASK_NAME, args=[str(job_id)])
        except (OperationalError, OSError, TimeoutError) as exc:
            async with SessionLocal() as session:
                async with session.begin():
                    job = await session.scalar(
                        select(ProcessingJob)
                        .where(ProcessingJob.id == job_id)
                        .with_for_update()
                    )
                    if job is not None and job.status == LifecycleStatus.QUEUED:
                        job.last_enqueued_at = None
            logger.warning(
                "Unable to republish a recoverable processing job",
                extra={"job_id": str(job_id), "error_type": type(exc).__name__},
            )
        else:
            dispatched += 1
    return dispatched
