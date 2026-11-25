from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.jobs import JobProgressResponse
from app.services.jobs.orchestration import (
    cancel_job,
    get_job_progress,
    retry_job,
    resume_quota_job,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobProgressResponse)
async def read_job(
    job_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobProgressResponse:
    return await get_job_progress(session, job_id)


@router.post("/{job_id}/cancel", response_model=JobProgressResponse)
async def cancel_processing_job(job_id: UUID) -> JobProgressResponse:
    return await cancel_job(job_id)


@router.post("/{job_id}/retry", response_model=JobProgressResponse)
async def retry_processing_job(job_id: UUID) -> JobProgressResponse:
    return await retry_job(job_id)


@router.post("/{job_id}/resume", response_model=JobProgressResponse)
async def resume_processing_job(job_id: UUID) -> JobProgressResponse:
    return await resume_quota_job(job_id)
