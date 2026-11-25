from typing import Annotated
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.config import get_settings
from app.db.session import get_db
from app.models.enums import LifecycleStatus
from app.models.processing_job import ProcessingJob
from app.models.video_asset import VideoAsset
from app.providers.errors import GenerationProviderError, ProviderError
from app.schemas.retrieval import (
    AnswerCitation,
    VideoAnswerRequest,
    VideoAnswerResponse,
    VideoSearchEvidence,
    VideoSearchRequest,
    VideoSearchResponse,
)
from app.schemas.videos import (
    PreflightRequest,
    PreflightResponse,
    UploadResponse,
    VideoDetailResponse,
    VideoLibraryItem,
    VideoLibraryResponse,
)
from app.services.media.audio import MediaProcessingError
from app.services.answering import answer_video_question
from app.services.retrieval import retrieve_video_evidence
from app.services.deduplication.service import action_for_status, find_asset_by_hash
from app.services.ingestion.uploads import save_video_upload
from app.schemas.jobs import ProcessingConsentRequest, ProcessingConsentResponse
from app.services.jobs.orchestration import (
    CONSENT_VERSION,
    get_job_progress,
    record_external_processing_consent,
)

router = APIRouter(prefix="/videos", tags=["videos"])


@router.get("", response_model=VideoLibraryResponse)
async def list_videos(
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> VideoLibraryResponse:
    assets = (
        await session.scalars(
            select(VideoAsset)
            .order_by(VideoAsset.created_at.desc(), VideoAsset.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return VideoLibraryResponse(
        videos=[
            VideoLibraryItem(
                id=asset.id,
                original_filename=asset.original_filename,
                file_size_bytes=asset.file_size_bytes,
                duration_seconds=asset.duration_seconds,
                status=asset.status,
                created_at=asset.created_at,
                updated_at=asset.updated_at,
            )
            for asset in assets
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/{asset_id}", response_model=VideoDetailResponse)
async def get_video(
    asset_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> VideoDetailResponse:
    asset = await session.get(VideoAsset, asset_id)
    if asset is None:
        raise APIError(404, "video_not_found", "The video asset was not found.")
    job = await session.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == asset_id)
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    )
    progress = await get_job_progress(session, job.id) if job else None
    return VideoDetailResponse(
        id=asset.id,
        original_filename=asset.original_filename,
        file_size_bytes=asset.file_size_bytes,
        duration_seconds=asset.duration_seconds,
        status=asset.status,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
        mime_type=asset.mime_type,
        video_codec=asset.video_codec,
        audio_codec=asset.audio_codec,
        width=asset.width,
        height=asset.height,
        external_processing_consent=(
            asset.external_processing_consent
            and asset.external_processing_consent_version == CONSENT_VERSION
        ),
        job=progress,
    )


@router.get("/{asset_id}/media")
async def stream_video(
    asset_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> FileResponse:
    asset = await session.get(VideoAsset, asset_id)
    if asset is None:
        raise APIError(404, "video_not_found", "The video asset was not found.")
    media_root = get_settings().media_root.resolve()
    media_path = (media_root / "originals" / str(asset_id) / "source.mp4").resolve()
    if (
        not media_path.is_relative_to(media_root)
        or Path(asset.original_path).resolve() != media_path
        or not media_path.is_file()
    ):
        raise APIError(404, "video_media_not_found", "The video file is unavailable.")
    return FileResponse(
        media_path,
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


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
        external_processing_consent=(
            asset.external_processing_consent
            and asset.external_processing_consent_version == CONSENT_VERSION
        ),
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


@router.post(
    "/{asset_id}/search",
    response_model=VideoSearchResponse,
)
async def search_video(
    asset_id: UUID,
    payload: VideoSearchRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> VideoSearchResponse:
    asset = await session.get(VideoAsset, asset_id)
    if asset is None:
        raise APIError(404, "video_not_found", "The video asset was not found.")
    if asset.status != LifecycleStatus.READY:
        raise APIError(
            409,
            "video_not_ready",
            "Search is available after video processing and index verification finish.",
        )
    if (
        not asset.external_processing_consent
        or asset.external_processing_consent_version != CONSENT_VERSION
    ):
        raise APIError(
            409,
            "external_processing_consent_required",
            "Accept the current external AI processing terms before searching.",
        )
    job_id = await session.scalar(
        select(ProcessingJob.id)
        .where(
            ProcessingJob.video_id == asset_id,
            ProcessingJob.status == LifecycleStatus.READY,
        )
        .order_by(ProcessingJob.created_at.desc())
    )
    if job_id is None:
        raise APIError(
            409,
            "ready_job_not_found",
            "The verified processing job for this video is unavailable.",
        )
    await session.rollback()

    try:
        result = await retrieve_video_evidence(
            job_id,
            asset_id,
            payload.query,
            limit=payload.limit,
        )
    except ProviderError as error:
        if error.quota:
            raise APIError(
                429,
                "provider_quota_exceeded",
                "The online embedding quota is temporarily unavailable. Try again later.",
            ) from error
        raise APIError(
            503 if error.transient else 502,
            "embedding_provider_unavailable"
            if error.transient
            else "embedding_request_failed",
            "The online embedding service could not process this search.",
        ) from error
    except MediaProcessingError as error:
        if error.code in {
            "embedding_index_not_available",
            "embedding_index_version_mismatch",
            "embedding_configuration_incomplete",
        }:
            status_code = 409
        elif error.transient:
            status_code = 503
        elif error.code == "query_input_invalid":
            status_code = 422
        else:
            status_code = 500
        raise APIError(status_code, error.code, error.user_message) from error

    return VideoSearchResponse(
        video_id=asset_id,
        evidence=[
            VideoSearchEvidence(
                citation_id=item.citation_id,
                chunk_id=item.id,
                chunk_type=item.chunk_type,
                content=item.content,
                start_time=item.start_time,
                end_time=item.end_time,
                source_ref=item.source_ref,
                vector_rank=item.vector_rank,
                keyword_rank=item.keyword_rank,
                rrf_score=item.rrf_score,
            )
            for item in result.evidence
        ],
        context_char_count=len(result.context),
    )


@router.post(
    "/{asset_id}/answer",
    response_model=VideoAnswerResponse,
)
async def answer_video(
    asset_id: UUID,
    payload: VideoAnswerRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> VideoAnswerResponse:
    asset = await session.get(VideoAsset, asset_id)
    if asset is None:
        raise APIError(404, "video_not_found", "The video asset was not found.")
    if asset.status != LifecycleStatus.READY:
        raise APIError(
            409,
            "video_not_ready",
            "Questions are available after video processing and index verification finish.",
        )
    if (
        not asset.external_processing_consent
        or asset.external_processing_consent_version != CONSENT_VERSION
    ):
        raise APIError(
            409,
            "external_processing_consent_required",
            "Accept the current external AI processing terms before asking a question.",
        )
    job_id = await session.scalar(
        select(ProcessingJob.id)
        .where(
            ProcessingJob.video_id == asset_id,
            ProcessingJob.status == LifecycleStatus.READY,
        )
        .order_by(ProcessingJob.created_at.desc())
    )
    if job_id is None:
        raise APIError(
            409,
            "ready_job_not_found",
            "The verified processing job for this video is unavailable.",
        )
    await session.rollback()

    try:
        answer, inference, evidence = await answer_video_question(
            job_id,
            asset_id,
            payload.question,
            limit=payload.limit,
        )
    except ProviderError as error:
        if error.quota:
            raise APIError(
                429,
                "provider_quota_exceeded",
                "The online free-tier quota is temporarily unavailable. Try again later.",
            ) from error
        operation = "generation" if isinstance(error, GenerationProviderError) else "embedding"
        raise APIError(
            503 if error.transient else 502,
            f"{operation}_provider_unavailable"
            if error.transient
            else f"{operation}_request_failed",
            f"The online {operation} service could not process this question.",
        ) from error
    except MediaProcessingError as error:
        if error.code in {
            "embedding_index_not_available",
            "embedding_index_version_mismatch",
            "embedding_configuration_incomplete",
        }:
            status_code = 409
        elif error.transient:
            status_code = 503
        else:
            status_code = 500
        raise APIError(status_code, error.code, error.user_message) from error

    citations: list[AnswerCitation] = []
    for item in evidence:
        if item.citation_id is None:
            raise APIError(
                500,
                "answer_citation_missing",
                "A verified answer citation could not be prepared.",
            )
        citations.append(
            AnswerCitation(
                citation_id=item.citation_id,
                chunk_id=item.id,
                chunk_type=item.chunk_type,
                content=item.content,
                start_time=item.start_time,
                end_time=item.end_time,
            )
        )

    return VideoAnswerResponse(
        video_id=asset_id,
        answer=answer,
        abstained=not bool(evidence),
        inference=inference,
        citations=citations,
    )
