import asyncio
import hashlib
import hmac
import logging
import os
import re
import shutil
import unicodedata
from pathlib import Path, PurePosixPath
from uuid import uuid4

import aiofiles
from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import APIError
from app.models.enums import (
    LifecycleStatus,
    PIPELINE_STAGES,
    StageStatus,
)
from app.models.processing_job import ProcessingJob
from app.models.processing_stage import ProcessingStage
from app.models.video_asset import VideoAsset
from app.services.deduplication.service import (
    action_for_status,
    find_asset_by_hash,
)
from app.services.media.ffprobe import inspect_mp4

logger = logging.getLogger(__name__)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ALLOWED_DECLARED_MIME_TYPES = {"video/mp4", "application/octet-stream"}


def sanitize_filename(filename: str) -> str:
    normalized = unicodedata.normalize("NFKC", filename.replace("\\", "/"))
    name = PurePosixPath(normalized).name
    name = "".join(character for character in name if character.isprintable()).strip()
    if not name or len(name) > 255 or name in {".", ".."}:
        raise APIError(422, "invalid_filename", "Provide a valid video filename.")
    if PurePosixPath(name).suffix.lower() != ".mp4":
        raise APIError(415, "unsupported_extension", "Only .mp4 files are supported.")
    return name


async def _write_and_hash(request: Request, destination: Path) -> tuple[int, str]:
    settings = get_settings()
    digest = hashlib.sha256()
    size = 0
    try:
        async with aiofiles.open(destination, "wb") as file:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise APIError(
                        413,
                        "file_too_large",
                        f"Files must be {settings.max_upload_bytes} bytes or smaller.",
                    )
                digest.update(chunk)
                await file.write(chunk)
    except OSError as exc:
        logger.error(
            "Unable to write the uploaded video",
            extra={"error_type": type(exc).__name__},
        )
        raise APIError(
            507,
            "storage_unavailable",
            "The video could not be saved to local storage.",
        ) from exc
    return size, digest.hexdigest()


async def _remove_staging(path: Path) -> None:
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError as exc:
        logger.error(
            "Unable to remove a temporary upload",
            extra={"error_type": type(exc).__name__},
        )
        raise APIError(
            507,
            "temporary_file_cleanup_failed",
            "Temporary upload cleanup failed; contact the local administrator.",
        ) from exc


async def save_video_upload(
    request: Request,
    session: AsyncSession,
    filename: str,
    declared_mime_type: str | None,
    client_sha256: str,
) -> tuple[VideoAsset, ProcessingJob | None, bool, str]:
    if not _SHA256_RE.fullmatch(client_sha256):
        raise APIError(
            422,
            "invalid_sha256",
            "X-Content-SHA256 must contain a 64-character hexadecimal digest.",
        )

    safe_filename = sanitize_filename(filename)
    mime_type = (declared_mime_type or "").split(";", maxsplit=1)[0].strip().lower()
    if mime_type not in _ALLOWED_DECLARED_MIME_TYPES:
        raise APIError(
            415,
            "unsupported_media_type",
            "Use video/mp4 or application/octet-stream for an MP4 upload.",
        )

    settings = get_settings()
    storage_root = settings.media_root.resolve()
    staging_directory = storage_root / ".staging"
    try:
        await asyncio.to_thread(staging_directory.mkdir, parents=True, exist_ok=True)
    except OSError as exc:
        logger.error(
            "Unable to create the upload staging directory",
            extra={"error_type": type(exc).__name__},
        )
        raise APIError(
            507,
            "storage_unavailable",
            "Local video storage is unavailable.",
        ) from exc

    staging_path = staging_directory / f"{uuid4().hex}.upload"
    final_directory: Path | None = None
    persisted = False
    try:
        size, server_sha256 = await _write_and_hash(request, staging_path)
        if size == 0:
            raise APIError(422, "empty_file", "The uploaded video is empty.")
        if not hmac.compare_digest(server_sha256, client_sha256.lower()):
            raise APIError(
                422,
                "hash_mismatch",
                "The uploaded bytes do not match the browser-provided SHA-256.",
            )

        existing, existing_job = await find_asset_by_hash(session, server_sha256)
        if existing is not None:
            return existing, existing_job, True, action_for_status(existing.status)

        media_info = await inspect_mp4(staging_path)
        asset_id = uuid4()
        final_directory = storage_root / "originals" / str(asset_id)
        final_path = final_directory / "source.mp4"
        try:
            await asyncio.to_thread(final_directory.mkdir, parents=True, exist_ok=False)
            await asyncio.to_thread(os.replace, staging_path, final_path)
        except OSError as exc:
            logger.error(
                "Unable to move the validated upload into canonical storage",
                extra={"error_type": type(exc).__name__},
            )
            raise APIError(
                507,
                "storage_unavailable",
                "The validated video could not be moved into local storage.",
            ) from exc

        asset = VideoAsset(
            id=asset_id,
            sha256_hash=server_sha256,
            original_path=str(final_path),
            original_filename=safe_filename,
            file_size_bytes=size,
            duration_seconds=media_info.duration_seconds,
            mime_type="video/mp4",
            video_codec=media_info.video_codec,
            audio_codec=media_info.audio_codec,
            width=media_info.width,
            height=media_info.height,
            frame_rate=media_info.frame_rate,
            status=LifecycleStatus.UPLOADED,
        )
        job = ProcessingJob(
            video_id=asset_id,
            status=LifecycleStatus.UPLOADED,
            current_stage="media_preparation",
            idempotency_key="initial-ingestion",
        )
        audio_stage_status = (
            StageStatus.PENDING
            if media_info.audio_codec is not None
            else StageStatus.SKIPPED
        )
        stages = [
            ProcessingStage(
                name=name,
                status=(
                    StageStatus.COMPLETED
                    if name == "media_validation"
                    else audio_stage_status
                    if name in {"audio_extraction", "transcription", "transcript_chunking"}
                    else StageStatus.PENDING
                ),
            )
            for name in PIPELINE_STAGES
        ]
        job.stages = stages

        await session.rollback()
        try:
            async with session.begin():
                session.add(asset)
                session.add(job)
                await session.flush()
        except IntegrityError:
            await session.rollback()
            existing, existing_job = await find_asset_by_hash(
                session,
                server_sha256,
            )
            if existing is None:
                raise
            return existing, existing_job, True, action_for_status(existing.status)

        persisted = True
        return asset, job, False, "uploaded"
    finally:
        if staging_path.exists():
            await _remove_staging(staging_path)
        if final_directory is not None and not persisted:
            try:
                await asyncio.to_thread(shutil.rmtree, final_directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.error(
                    "Unable to clean up an uncommitted video file",
                    extra={"error_type": type(exc).__name__},
                )
                raise APIError(
                    507,
                    "storage_cleanup_failed",
                    "An uncommitted video file could not be removed.",
                ) from exc
