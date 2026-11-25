from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.db.session import get_db
from app.schemas.videos import PreflightRequest, PreflightResponse, UploadResponse
from app.services.deduplication.service import action_for_status, find_asset_by_hash
from app.services.ingestion.uploads import save_video_upload
from app.schemas.jobs import ProcessingConsentRequest, ProcessingConsentResponse
from app.services.jobs.orchestration import (
    CONSENT_VERSION,
    record_external_processing_consent,
)

router = APIRouter(prefix="/videos", tags=["videos"])


@router.post("/preflight", response_model=PreflightResponse)
async def upload_preflight(
    payload: PreflightRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PreflightResponse:
    asset, job = await find_asset_by_hash(session, payload.sha256_hash)
    if asset is None:
        return PreflightResponse(can_upload=True, duplicate_action="upload")

    action = action_for_status(asset.status)
    return PreflightResponse(
        can_upload=False,
        duplicate_action=action,
        asset_id=asset.id,
        job_id=job.id if job else None,
        status=asset.status,
        retry_available=action == "retry_available",
        external_processing_consent=asset.external_processing_consent,
    )


@router.post("/upload", response_model=UploadResponse)
async def upload_video(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    x_content_sha256: Annotated[str, Header()],
    x_external_processing_consent: Annotated[bool, Header()] = False,
    content_type: Annotated[str | None, Header()] = None,
) -> UploadResponse:
    asset, job, duplicate, action = await save_video_upload(
        request=request,
        session=session,
        filename=filename,
        declared_mime_type=content_type,
        client_sha256=x_content_sha256,
        external_processing_consent=x_external_processing_consent,
    )
    if job is None:
        raise APIError(
            500,
            "processing_job_missing",
            "The canonical video has no processing job.",
        )
    status = await record_external_processing_consent(asset.id)
    asset.status = status
    if duplicate:
        action = action_for_status(status)
    return UploadResponse(
        asset_id=asset.id,
        job_id=job.id,
        sha256_hash=asset.sha256_hash,
        original_filename=asset.original_filename,
        file_size_bytes=asset.file_size_bytes,
        duration_seconds=asset.duration_seconds,
        status=asset.status,
        duplicate=duplicate,
        duplicate_action=action,
    )


@router.post(
    "/{asset_id}/consent",
    response_model=ProcessingConsentResponse,
)
async def record_processing_consent(
    asset_id: UUID,
    payload: ProcessingConsentRequest,
) -> ProcessingConsentResponse:
    if not payload.accepted:
        raise APIError(
            422,
            "external_processing_consent_required",
            "Accept the external AI processing terms to continue.",
        )
    status = await record_external_processing_consent(asset_id)
    return ProcessingConsentResponse(
        asset_id=asset_id,
        accepted=True,
        status=status,
        consent_version=CONSENT_VERSION,
    )
