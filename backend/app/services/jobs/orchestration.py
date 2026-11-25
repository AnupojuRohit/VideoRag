from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import APIError
from app.core.privacy import EXTERNAL_PROCESSING_CONSENT_VERSION
from app.db.session import SessionLocal
from app.models.enums import LifecycleStatus, StageStatus, PIPELINE_STAGES
from app.models.processing_job import ProcessingJob
from app.models.processing_stage import ProcessingStage
from app.models.video_asset import VideoAsset
from app.schemas.jobs import JobProgressResponse, StageProgress
from app.services.jobs.dispatch import schedule_job

CONSENT_VERSION = EXTERNAL_PROCESSING_CONSENT_VERSION
_ACTIVE_STATUSES = {
    LifecycleStatus.UPLOADED,
    LifecycleStatus.QUEUED,
    LifecycleStatus.PROCESSING,
    LifecycleStatus.INDEXING,
    LifecycleStatus.VERIFYING,
    LifecycleStatus.PAUSED_QUOTA,
}


async def get_job_progress(
    session: AsyncSession,
    job_id: UUID,
) -> JobProgressResponse:
    job = await session.get(ProcessingJob, job_id)
    if job is None:
        raise APIError(404, "job_not_found", "The processing job was not found.")

    stages = list(
        (
            await session.scalars(
                select(ProcessingStage)
                .where(ProcessingStage.job_id == job_id)
                .order_by(ProcessingStage.created_at, ProcessingStage.name)
            )
        ).all()
    )
    completed_count = sum(
        stage.status in {StageStatus.COMPLETED, StageStatus.SKIPPED}
        for stage in stages
    )
    stage_by_name = {stage.name: stage for stage in stages}
    stage_progress = []
    for name in PIPELINE_STAGES:
        stage = stage_by_name.get(name)
        if stage is None:
            stage_progress.append(
                StageProgress(
                    name=name,
                    status=StageStatus.PENDING,
                    attempt_count=0,
                    started_at=None,
                    finished_at=None,
                    error_code=None,
                    error_message=None,
                )
            )
            continue
        error = stage.last_error or {}
        stage_progress.append(
            StageProgress(
                name=stage.name,
                status=stage.status,
                attempt_count=stage.attempt_count,
                started_at=stage.started_at,
                finished_at=stage.finished_at,
                error_code=error.get("code"),
                error_message=error.get("user_message"),
            )
        )

    retry_available = job.attempt_count < get_settings().job_max_attempts and (
        job.status == LifecycleStatus.CANCELLED
        or (
            job.status == LifecycleStatus.FAILED
            and bool((job.last_error or {}).get("retryable"))
        )
    )
    cancel_available = job.status in _ACTIVE_STATUSES
    return JobProgressResponse(
        id=job.id,
        video_id=job.video_id,
        status=job.status,
        current_stage=job.current_stage,
        attempt_count=job.attempt_count,
        completed_stages=completed_count,
        total_stages=len(PIPELINE_STAGES),
        progress_percent=round(completed_count * 100 / len(PIPELINE_STAGES)),
        started_at=job.started_at,
        finished_at=job.finished_at,
        resume_after=job.resume_after,
        retry_available=retry_available,
        cancel_available=cancel_available,
        stages=stage_progress,
    )


async def record_external_processing_consent(
    asset_id: UUID,
) -> LifecycleStatus:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            asset = await session.scalar(
                select(VideoAsset)
                .where(VideoAsset.id == asset_id)
                .with_for_update()
            )
            if asset is None:
                raise APIError(404, "video_not_found", "The video asset was not found.")
            asset.external_processing_consent = True
            asset.external_processing_consent_at = now
            asset.external_processing_consent_version = CONSENT_VERSION
            job = await session.scalar(
                select(ProcessingJob)
                .where(
                    ProcessingJob.video_id == asset.id,
                    ProcessingJob.status.in_(
                        [
                            LifecycleStatus.UPLOADED,
                            LifecycleStatus.QUEUED,
                            LifecycleStatus.PROCESSING,
                            LifecycleStatus.INDEXING,
                            LifecycleStatus.VERIFYING,
                            LifecycleStatus.PAUSED_QUOTA,
                        ]
                    ),
                )
                .order_by(ProcessingJob.created_at.desc())
            )
            status = job.status if job is not None else asset.status
            job_id = job.id if job is not None else None
    if job_id is not None:
        scheduled_status = await schedule_job(job_id)
        if scheduled_status is not None:
            status = scheduled_status
    return status


async def cancel_job(job_id: UUID) -> JobProgressResponse:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.id == job_id)
                .with_for_update()
            )
            if job is None:
                raise APIError(404, "job_not_found", "The processing job was not found.")
            if job.status not in _ACTIVE_STATUSES:
                raise APIError(
                    409,
                    "job_not_cancellable",
                    "This processing job is no longer active.",
                )
            asset = await session.get(VideoAsset, job.video_id)
            job.status = LifecycleStatus.CANCELLED
            job.finished_at = now
            job.lease_expires_at = None
            job.last_enqueued_at = None
            job.resume_after = None
            if asset is not None:
                asset.status = LifecycleStatus.CANCELLED
            stages = list(
                (
                    await session.scalars(
                        select(ProcessingStage)
                        .where(
                            ProcessingStage.job_id == job.id,
                            ProcessingStage.status.in_(
                                [
                                    StageStatus.PENDING,
                                    StageStatus.RUNNING,
                                    StageStatus.PAUSED_QUOTA,
                                ]
                            ),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for stage in stages:
                stage.status = StageStatus.CANCELLED
                stage.finished_at = now

    async with SessionLocal() as session:
        return await get_job_progress(session, job_id)


async def retry_job(
    job_id: UUID,
    *,
    allow_quota_resume: bool = False,
) -> JobProgressResponse:
    now = datetime.now(timezone.utc)
    settings = get_settings()
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.id == job_id)
                .with_for_update()
            )
            if job is None:
                raise APIError(404, "job_not_found", "The processing job was not found.")
            asset = await session.get(VideoAsset, job.video_id)
            if asset is None:
                raise APIError(404, "video_not_found", "The video asset was not found.")
            if (
                not asset.external_processing_consent
                or asset.external_processing_consent_version != CONSENT_VERSION
            ):
                raise APIError(
                    409,
                    "external_processing_consent_required",
                    "Accept the external AI processing terms before retrying this job.",
                )
            if (
                job.status != LifecycleStatus.PAUSED_QUOTA
                and job.attempt_count >= settings.job_max_attempts
            ):
                raise APIError(
                    409,
                    "job_retry_limit_reached",
                    "This job has reached its configured attempt limit.",
                )

            if allow_quota_resume:
                if job.status != LifecycleStatus.PAUSED_QUOTA:
                    raise APIError(
                        409,
                        "job_not_resumable",
                        "Only a quota-paused job can be resumed.",
                    )
                if job.resume_after is not None and job.resume_after > now:
                    raise APIError(
                        409,
                        "quota_cooldown_active",
                        "The free-tier quota cooldown is still active.",
                    )
            elif job.status == LifecycleStatus.PAUSED_QUOTA:
                raise APIError(
                    409,
                    "job_not_retryable",
                    "Use the resume action to continue a quota-paused job.",
                )
            elif job.status == LifecycleStatus.FAILED:
                if not bool((job.last_error or {}).get("retryable")):
                    raise APIError(
                        409,
                        "job_not_retryable",
                        "This failure is not safe to retry. Check the stage error.",
                    )
            elif job.status != LifecycleStatus.CANCELLED:
                raise APIError(
                    409,
                    "job_not_retryable",
                    "Only failed, cancelled, or cooldown-ready quota-paused jobs can resume.",
                )

            stages = list(
                (
                    await session.scalars(
                        select(ProcessingStage)
                        .where(
                            ProcessingStage.job_id == job.id,
                            ProcessingStage.status.in_(
                                [
                                    StageStatus.FAILED,
                                    StageStatus.CANCELLED,
                                    StageStatus.PAUSED_QUOTA,
                                ]
                            ),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for stage in stages:
                stage.status = StageStatus.PENDING
                stage.finished_at = None
                stage.last_error = None
            job.status = LifecycleStatus.QUEUED
            job.last_error = None
            job.finished_at = None
            job.resume_after = None
            job.lease_expires_at = None
            job.last_enqueued_at = None
            asset.status = LifecycleStatus.QUEUED
            asset.updated_at = now

    await schedule_job(job_id)
    async with SessionLocal() as session:
        return await get_job_progress(session, job_id)


async def resume_quota_job(job_id: UUID) -> JobProgressResponse:
    return await retry_job(job_id, allow_quota_resume=True)


async def pause_job_for_quota(
    job_id: UUID,
    stage_name: str,
    resume_after: datetime | None,
    error_code: str,
    user_message: str,
) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.id == job_id)
                .with_for_update()
            )
            if job is None or job.status == LifecycleStatus.CANCELLED:
                return
            stage = await session.scalar(
                select(ProcessingStage)
                .where(
                    ProcessingStage.job_id == job_id,
                    ProcessingStage.name == stage_name,
                )
                .with_for_update()
            )
            if stage is not None:
                stage.status = StageStatus.PAUSED_QUOTA
                stage.finished_at = now
                stage.last_error = {
                    "code": error_code,
                    "user_message": user_message,
                    "retryable": True,
                }
            job.status = LifecycleStatus.PAUSED_QUOTA
            job.current_stage = stage_name
            job.last_error = {
                "code": error_code,
                "user_message": user_message,
                "retryable": True,
            }
            job.resume_after = resume_after
            job.finished_at = None
            job.lease_expires_at = None
            job.last_enqueued_at = None
            asset = await session.get(VideoAsset, job.video_id)
            if asset is not None:
                asset.status = LifecycleStatus.PAUSED_QUOTA


async def mark_job_failed(
    job_id: UUID,
    stage_name: str,
    error_code: str,
    user_message: str,
    retryable: bool,
) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.id == job_id)
                .with_for_update()
            )
            if job is None or job.status == LifecycleStatus.CANCELLED:
                return
            stage = await session.scalar(
                select(ProcessingStage)
                .where(
                    ProcessingStage.job_id == job_id,
                    ProcessingStage.name == stage_name,
                )
                .with_for_update()
            )
            if stage is not None:
                stage.status = StageStatus.FAILED
                stage.finished_at = now
                stage.last_error = {
                    "code": error_code,
                    "user_message": user_message,
                    "retryable": retryable,
                }
            job.status = LifecycleStatus.FAILED
            job.current_stage = stage_name
            job.last_error = {
                "code": error_code,
                "user_message": user_message,
                "retryable": retryable,
            }
            job.finished_at = now
            job.lease_expires_at = None
            job.last_enqueued_at = None
            asset = await session.get(VideoAsset, job.video_id)
            if asset is not None:
                asset.status = LifecycleStatus.FAILED
