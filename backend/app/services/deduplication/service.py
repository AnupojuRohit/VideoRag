from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import LifecycleStatus
from app.models.processing_job import ProcessingJob
from app.models.video_asset import VideoAsset


async def find_asset_by_hash(
    session: AsyncSession,
    sha256_hash: str,
) -> tuple[VideoAsset | None, ProcessingJob | None]:
    asset = await session.scalar(
        select(VideoAsset).where(VideoAsset.sha256_hash == sha256_hash)
    )
    if asset is None:
        return None, None
    job = await session.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == asset.id)
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    )
    return asset, job


def action_for_status(status: LifecycleStatus) -> str:
    if status == LifecycleStatus.READY:
        return "reuse_ready"
    if status == LifecycleStatus.PAUSED_QUOTA:
        return "reuse_paused_quota"
    if status in {LifecycleStatus.FAILED, LifecycleStatus.CANCELLED}:
        return "retry_available"
    if status == LifecycleStatus.UPLOADED:
        return "reuse_uploaded"
    return "attach_existing_job"
