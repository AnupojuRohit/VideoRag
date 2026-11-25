import asyncio
import json
import logging
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import aiofiles
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.privacy import EXTERNAL_PROCESSING_CONSENT_VERSION
from app.db.session import SessionLocal, engine
from app.models.enums import (
    ChunkType,
    LifecycleStatus,
    PIPELINE_STAGES,
    StageStatus,
)
from app.models.processing_job import ProcessingJob
from app.models.processing_stage import ProcessingStage
from app.models.provider_usage_event import ProviderUsageEvent
from app.models.video_asset import VideoAsset
from app.providers.errors import ProviderError, TranscriptionProviderError
from app.providers.gemini_transcription import (
    MODEL_ID as TRANSCRIPTION_MODEL_ID,
    create_transcription_provider,
)
from app.providers.gemini_vision import (
    MODEL_ID as VISION_MODEL_ID,
    create_vision_provider,
)
from app.providers.transcription import (
    ProviderUsage,
    TranscribedWord,
    TranscriptionProvider,
)
from app.providers.vision import VisionProvider
from app.services.jobs.orchestration import (
    mark_job_failed,
    pause_job_for_quota,
)
from app.services.indexing.shared import (
    SearchableChunk,
    index_evidence,
    normalize_evidence,
    prune_stale_evidence,
    verify_video_index,
)
from app.services.media.audio import (
    AudioSegment,
    MediaProcessingError,
    build_audio_segments,
    extract_audio_segment,
    extract_audio_track,
    inspect_audio_duration,
)
from app.services.media.scene_detection import DetectedScene, detect_scenes
from app.services.media.video_frames import (
    SelectedFrame,
    extract_selected_frames,
    near_duplicate_frames,
    select_frame_timestamps,
)
from app.services.visual.ocr import extract_visible_text

logger = logging.getLogger(__name__)
_STAGE_NAMES = PIPELINE_STAGES
_WORD_CHUNK_LIMIT = 1000
_PAUSE_CHUNK_SECONDS = 2.5
_WORD_SPACE_PUNCTUATION = re.compile(r"^[,.;:!?%)\]}]+$")


@dataclass(frozen=True)
class JobContext:
    job_id: UUID
    video_id: UUID
    original_path: Path
    duration_seconds: float
    audio_start_time: float
    has_audio: bool
    work_dir: Path


class RetryableJobError(Exception):
    def __init__(self, message: str, countdown: int) -> None:
        super().__init__(message)
        self.countdown = countdown


async def _try_job_lock(job_id: UUID):
    connection = await engine.connect()
    try:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:job_id, 0))"),
            {"job_id": str(job_id)},
        )
        await connection.commit()
    except SQLAlchemyError:
        await connection.close()
        raise
    if not acquired:
        await connection.close()
        return None
    return connection


async def _release_job_lock(connection, job_id: UUID) -> None:
    try:
        await connection.execute(
            text("SELECT pg_advisory_unlock(hashtextextended(:job_id, 0))"),
            {"job_id": str(job_id)},
        )
        await connection.commit()
    finally:
        await connection.close()


async def _claim_job(job_id: UUID) -> JobContext | None:
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
                logger.warning("Processing task referenced an unknown job")
                return None
            if job.status in {
                LifecycleStatus.CANCELLED,
                LifecycleStatus.FAILED,
                LifecycleStatus.READY,
                LifecycleStatus.PAUSED_QUOTA,
            }:
                return None
            if job.status == LifecycleStatus.PROCESSING and (
                job.lease_expires_at is None or job.lease_expires_at > now
            ):
                return None

            asset = await session.scalar(
                select(VideoAsset)
                .where(VideoAsset.id == job.video_id)
                .with_for_update()
            )
            if asset is None:
                raise MediaProcessingError(
                    "video_asset_missing",
                    "The video asset for this job no longer exists.",
                    retryable=False,
                )
            if (
                not asset.external_processing_consent
                or asset.external_processing_consent_version
                != EXTERNAL_PROCESSING_CONSENT_VERSION
            ):
                raise TranscriptionProviderError(
                    "external_processing_consent_required",
                    "Accept the external AI processing terms before this job can run.",
                    retryable=False,
                )

            storage_root = settings.media_root.resolve()
            original_path = Path(asset.original_path).resolve()
            original_root = (storage_root / "originals").resolve()
            if not original_path.is_relative_to(original_root):
                raise MediaProcessingError(
                    "media_path_invalid",
                    "The stored video path is outside the configured media directory.",
                    retryable=False,
                )
            work_dir = storage_root / "work" / str(asset.id)

            job.status = LifecycleStatus.PROCESSING
            job.attempt_count += 1
            job.started_at = job.started_at or now
            job.finished_at = None
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(
                seconds=settings.job_lease_seconds
            )
            job.last_enqueued_at = None
            job.last_error = None
            asset.status = LifecycleStatus.PROCESSING

            for name in _STAGE_NAMES:
                stage = await session.scalar(
                    select(ProcessingStage).where(
                        ProcessingStage.job_id == job.id,
                        ProcessingStage.name == name,
                    )
                )
                if stage is None:
                    session.add(
                        ProcessingStage(
                            job_id=job.id,
                            name=name,
                            status=StageStatus.PENDING,
                        )
                    )

            return JobContext(
                job_id=job.id,
                video_id=asset.id,
                original_path=original_path,
                duration_seconds=asset.duration_seconds,
                audio_start_time=asset.audio_start_time or 0.0,
                has_audio=asset.audio_codec is not None,
                work_dir=work_dir,
            )


async def _heartbeat(job_id: UUID, stop: asyncio.Event) -> None:
    interval = max(5, get_settings().job_lease_seconds // 3)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            now = datetime.now(timezone.utc)
            async with SessionLocal() as session:
                async with session.begin():
                    job = await session.scalar(
                        select(ProcessingJob)
                        .where(ProcessingJob.id == job_id)
                        .with_for_update()
                    )
                    if (
                        job is None
                        or job.status != LifecycleStatus.PROCESSING
                        or job.current_stage is None
                    ):
                        return
                    job.heartbeat_at = now
                    job.lease_expires_at = now + timedelta(
                        seconds=get_settings().job_lease_seconds
                    )


async def _is_cancelled(job_id: UUID) -> bool:
    async with SessionLocal() as session:
        status = await session.scalar(
            select(ProcessingJob.status).where(ProcessingJob.id == job_id)
        )
    return status in {LifecycleStatus.CANCELLED, None}


async def _start_stage(job_id: UUID, stage_name: str) -> bool:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.id == job_id)
                .with_for_update()
            )
            if job is None or job.status == LifecycleStatus.CANCELLED:
                return False
            stage = await session.scalar(
                select(ProcessingStage)
                .where(
                    ProcessingStage.job_id == job_id,
                    ProcessingStage.name == stage_name,
                )
                .with_for_update()
            )
            if stage is None:
                stage = ProcessingStage(job_id=job_id, name=stage_name)
                session.add(stage)
                await session.flush()
            if stage.status in {StageStatus.COMPLETED, StageStatus.SKIPPED}:
                return False
            stage.status = StageStatus.RUNNING
            stage.attempt_count += 1
            stage.started_at = stage.started_at or now
            stage.finished_at = None
            stage.last_error = None
            job.current_stage = stage_name
            job.heartbeat_at = now
            return True


async def _complete_stage(
    job_id: UUID,
    stage_name: str,
    checkpoint: dict[str, Any],
) -> bool:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.id == job_id)
                .with_for_update()
            )
            if job is None or job.status == LifecycleStatus.CANCELLED:
                return False
            stage = await session.scalar(
                select(ProcessingStage)
                .where(
                    ProcessingStage.job_id == job_id,
                    ProcessingStage.name == stage_name,
                )
                .with_for_update()
            )
            if stage is None:
                return False
            stage.status = StageStatus.COMPLETED
            stage.finished_at = now
            stage.checkpoint = checkpoint
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(
                seconds=get_settings().job_lease_seconds
            )
    return True


async def _save_stage_checkpoint(
    job_id: UUID,
    stage_name: str,
    checkpoint: dict[str, Any],
) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            stage = await session.scalar(
                select(ProcessingStage)
                .where(
                    ProcessingStage.job_id == job_id,
                    ProcessingStage.name == stage_name,
                )
                .with_for_update()
            )
            if stage is not None:
                stage.checkpoint = checkpoint


async def _stage_checkpoint(job_id: UUID, stage_name: str) -> dict[str, Any]:
    async with SessionLocal() as session:
        checkpoint = await session.scalar(
            select(ProcessingStage.checkpoint).where(
                ProcessingStage.job_id == job_id,
                ProcessingStage.name == stage_name,
            )
        )
    return checkpoint or {}


async def _reset_stage(job_id: UUID, stage_name: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            stage = await session.scalar(
                select(ProcessingStage)
                .where(
                    ProcessingStage.job_id == job_id,
                    ProcessingStage.name == stage_name,
                )
                .with_for_update()
            )
            if stage is not None and stage.status in {
                StageStatus.COMPLETED,
                StageStatus.FAILED,
            }:
                stage.status = StageStatus.PENDING
                stage.finished_at = None
                stage.checkpoint = {}


def _segment_artifact_exists(
    context: JobContext,
    item: Any,
    suffix: str,
) -> bool:
    if not isinstance(item, dict):
        return False
    try:
        index = int(item["index"])
    except (KeyError, TypeError, ValueError):
        return False
    if index < 0:
        return False
    return (
        context.work_dir
        / "audio"
        / f"segment-{index:05d}.{suffix}"
    ).is_file()


async def _set_lifecycle_status(
    job_id: UUID,
    status: LifecycleStatus,
) -> None:
    now = datetime.now(timezone.utc)
    try:
        async with SessionLocal() as session:
            async with session.begin():
                job = await session.scalar(
                    select(ProcessingJob)
                    .where(ProcessingJob.id == job_id)
                    .with_for_update()
                )
                if job is None or job.status == LifecycleStatus.CANCELLED:
                    return
                asset = await session.scalar(
                    select(VideoAsset)
                    .where(VideoAsset.id == job.video_id)
                    .with_for_update()
                )
                if asset is None:
                    raise MediaProcessingError(
                        "video_asset_missing",
                        "The video asset for this job no longer exists.",
                        retryable=False,
                    )
                job.status = status
                job.heartbeat_at = now
                job.lease_expires_at = now + timedelta(
                    seconds=get_settings().job_lease_seconds
                )
                asset.status = status
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "job_status_update_failed",
            "The processing status could not be updated.",
            retryable=True,
            transient=True,
        ) from exc


async def _mark_job_ready(job_id: UUID) -> None:
    now = datetime.now(timezone.utc)
    try:
        async with SessionLocal() as session:
            async with session.begin():
                job = await session.scalar(
                    select(ProcessingJob)
                    .where(ProcessingJob.id == job_id)
                    .with_for_update()
                )
                if job is None or job.status == LifecycleStatus.CANCELLED:
                    return
                statuses = (
                    await session.scalars(
                        select(ProcessingStage.status).where(
                            ProcessingStage.job_id == job_id
                        )
                    )
                ).all()
                if len(statuses) != len(_STAGE_NAMES) or any(
                    status not in {StageStatus.COMPLETED, StageStatus.SKIPPED}
                    for status in statuses
                ):
                    raise MediaProcessingError(
                        "required_processing_incomplete",
                        "The evidence index cannot be marked ready before every required stage is complete.",
                        retryable=True,
                    )
                asset = await session.scalar(
                    select(VideoAsset)
                    .where(VideoAsset.id == job.video_id)
                    .with_for_update()
                )
                if asset is None:
                    raise MediaProcessingError(
                        "video_asset_missing",
                        "The video asset for this job no longer exists.",
                        retryable=False,
                    )
                job.status = LifecycleStatus.READY
                job.current_stage = None
                job.finished_at = now
                job.heartbeat_at = now
                job.lease_expires_at = None
                job.last_enqueued_at = None
                job.last_error = None
                asset.status = LifecycleStatus.READY
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "job_ready_update_failed",
            "The verified asset could not be marked ready.",
            retryable=True,
            transient=True,
        ) from exc


async def _verify_index_artifacts(context: JobContext) -> dict[str, Any]:
    expected: list[SearchableChunk] = []
    if context.has_audio:
        try:
            audio_payload = await _read_json(
                context.work_dir / "audio" / "transcript-chunks.json"
            )
        except MediaProcessingError as error:
            raise MediaProcessingError(
                "transcript_artifact_invalid",
                "The saved transcript chunks are unavailable for verification.",
                retryable=True,
                transient=True,
            ) from error
        audio_items = audio_payload.get("chunks")
        if not isinstance(audio_items, list):
            raise MediaProcessingError(
                "transcript_artifact_invalid",
                "The saved transcript chunks are invalid.",
                retryable=False,
            )
        expected.extend(normalize_evidence(audio_items, ChunkType.TRANSCRIPT))

    try:
        visual_payload = await _read_json(
            context.work_dir / "visual" / "visual-evidence.json"
        )
    except MediaProcessingError as error:
        raise MediaProcessingError(
            "visual_evidence_invalid",
            "The saved visual evidence is unavailable for verification.",
            retryable=True,
            transient=True,
        ) from error
    visual_items = visual_payload.get("evidence")
    if not isinstance(visual_items, list):
        raise MediaProcessingError(
            "visual_evidence_invalid",
            "The saved visual evidence is invalid.",
            retryable=False,
        )
    expected.extend(normalize_evidence(visual_items, ChunkType.VISUAL))
    result = await verify_video_index(context.video_id, expected)
    return {
        **result,
        "audio_chunk_count": sum(
            chunk.chunk_type == ChunkType.TRANSCRIPT for chunk in expected
        ),
        "visual_chunk_count": sum(
            chunk.chunk_type == ChunkType.VISUAL for chunk in expected
        ),
    }


async def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        async with aiofiles.open(temporary_path, "w", encoding="utf-8") as file:
            await file.write(json.dumps(payload, ensure_ascii=False))
            await file.flush()
        await asyncio.to_thread(temporary_path.replace, path)
    except OSError as exc:
        raise MediaProcessingError(
            "local_artifact_write_failed",
            "A local processing artifact could not be saved.",
            retryable=True,
            transient=True,
        ) from exc


async def _read_json(path: Path) -> dict[str, Any]:
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as file:
            payload = json.loads(await file.read())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MediaProcessingError(
            "transcript_artifact_invalid",
            "A saved transcript checkpoint could not be read.",
            retryable=True,
            transient=True,
        ) from exc
    if not isinstance(payload, dict):
        raise MediaProcessingError(
            "transcript_artifact_invalid",
            "A saved transcript checkpoint is incomplete.",
            retryable=False,
        )
    return payload


async def _persist_usage(job_id: UUID, stage_name: str, usage: ProviderUsage) -> None:
    try:
        async with SessionLocal() as session:
            async with session.begin():
                session.add(
                    ProviderUsageEvent(
                        job_id=job_id,
                        stage_name=stage_name,
                        provider=usage.provider,
                        model_id=usage.model_id,
                        operation=usage.operation,
                        status_code=usage.status_code,
                        latency_ms=usage.latency_ms,
                        input_bytes=usage.input_bytes,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        request_id=usage.request_id,
                        error_code=usage.cleanup_error,
                    )
                )
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "provider_usage_write_failed",
            "Provider usage could not be recorded.",
            retryable=True,
            transient=True,
        ) from exc


async def _persist_provider_error(
    job_id: UUID,
    stage_name: str,
    source_path: Path | None,
    error: ProviderError,
    *,
    model_id: str,
    operation: str,
) -> None:
    try:
        input_bytes = source_path.stat().st_size if source_path is not None else None
    except OSError:
        input_bytes = None
    try:
        async with SessionLocal() as session:
            async with session.begin():
                session.add(
                    ProviderUsageEvent(
                        job_id=job_id,
                        stage_name=stage_name,
                        provider="gemini",
                        model_id=model_id,
                        operation=operation,
                        status_code=error.status_code,
                        latency_ms=error.latency_ms or 0,
                        input_bytes=input_bytes,
                        request_id=error.request_id,
                        error_code=error.code,
                    )
                )
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "provider_usage_write_failed",
            "Provider usage could not be recorded.",
            retryable=True,
            transient=True,
        ) from exc


async def _run_media_preparation(context: JobContext) -> dict[str, Any]:
    if not context.original_path.is_file():
        raise MediaProcessingError(
            "source_video_missing",
            "The original video file is missing from local storage.",
            retryable=False,
        )
    await asyncio.to_thread(context.work_dir.mkdir, parents=True, exist_ok=True)
    return {"work_dir": str(context.work_dir)}


async def _prepare_audio(context: JobContext) -> tuple[Path, float]:
    audio_path = context.work_dir / "audio" / "audio.flac"
    await extract_audio_track(context.original_path, audio_path)
    duration = await inspect_audio_duration(audio_path)
    return audio_path, duration


async def _create_audio_segments(
    context: JobContext,
    audio_path: Path,
    audio_duration: float,
) -> list[AudioSegment]:
    settings = get_settings()
    usable_duration = min(
        audio_duration,
        max(0.0, context.duration_seconds - context.audio_start_time),
    )
    if usable_duration <= 0:
        raise MediaProcessingError(
            "audio_timeline_invalid",
            "The audio track does not overlap the original video timeline.",
            retryable=False,
        )
    segments = build_audio_segments(
        audio_path,
        usable_duration,
        settings.audio_segment_seconds,
        settings.audio_segment_overlap_seconds,
    )
    for segment in segments:
        if await _is_cancelled(context.job_id):
            return []
        await extract_audio_segment(audio_path, segment)
    return segments


def _validate_segment_words(
    words: list[TranscribedWord],
    segment: AudioSegment,
    original_offset: float,
    video_duration: float,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for word in words:
        if (
            not math.isfinite(word.start_seconds)
            or not math.isfinite(word.end_seconds)
            or word.start_seconds < 0
            or word.end_seconds < word.start_seconds
            or word.end_seconds > segment.duration_seconds + 1
        ):
            raise TranscriptionProviderError(
                "provider_response_invalid",
                "The transcription provider returned invalid word timestamps.",
                retryable=False,
            )
        start = min(
            video_duration,
            original_offset + segment.start_seconds + word.start_seconds,
        )
        end = min(
            video_duration,
            original_offset + segment.start_seconds + word.end_seconds,
        )
        if end < start:
            continue
        normalized.append(
            {
                "text": word.text,
                "start_time": start,
                "end_time": end,
                "speaker": word.speaker,
            }
        )
    return normalized


async def _transcribe_segments(context: JobContext) -> None:
    checkpoint = await _stage_checkpoint(context.job_id, "audio_segmentation")
    raw_segments = checkpoint.get("segments", [])
    if not isinstance(raw_segments, list) or not raw_segments:
        raise MediaProcessingError(
            "audio_checkpoint_invalid",
            "The saved audio segment checkpoint is invalid.",
            retryable=False,
        )
    provider: TranscriptionProvider | None = None
    stage_checkpoint = await _stage_checkpoint(context.job_id, "transcription")
    completed_values = stage_checkpoint.get("completed_segments", [])
    if not isinstance(completed_values, list):
        raise MediaProcessingError(
            "transcription_checkpoint_invalid",
            "The saved transcription checkpoint is invalid.",
            retryable=False,
        )
    completed = set(completed_values)
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise MediaProcessingError(
                "audio_checkpoint_invalid",
                "The saved audio segment checkpoint is invalid.",
                retryable=False,
            )
        try:
            segment_index = int(raw_segment["index"])
            segment_start = float(raw_segment["start_seconds"])
            segment_duration = float(raw_segment["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaProcessingError(
                "audio_checkpoint_invalid",
                "The saved audio segment checkpoint is invalid.",
                retryable=False,
            ) from exc
        if segment_index < 0 or not math.isfinite(segment_start + segment_duration):
            raise MediaProcessingError(
                "audio_checkpoint_invalid",
                "The saved audio segment checkpoint is invalid.",
                retryable=False,
            )
        if await _is_cancelled(context.job_id):
            return
        segment = AudioSegment(
            index=segment_index,
            path=context.work_dir
            / "audio"
            / f"segment-{segment_index:05d}.flac",
            start_seconds=segment_start,
            duration_seconds=segment_duration,
        )
        transcript_path = segment.path.with_suffix(".transcript.json")
        if segment.index in completed:
            continue
        if transcript_path.is_file():
            try:
                artifact = await _read_json(transcript_path)
                if artifact.get("segment_index") == segment.index:
                    completed.add(segment.index)
                    stage_checkpoint = {
                        **stage_checkpoint,
                        "completed_segments": sorted(completed),
                    }
                    await _save_stage_checkpoint(
                        context.job_id,
                        "transcription",
                        stage_checkpoint,
                    )
                    continue
            except MediaProcessingError:
                pass

        try:
            if provider is None:
                provider = create_transcription_provider()
            result = await provider.transcribe(segment.path, "audio/flac")
        except TranscriptionProviderError as exc:
            await _persist_provider_error(
                context.job_id,
                "transcription",
                segment.path,
                exc,
                model_id=TRANSCRIPTION_MODEL_ID,
                operation="transcription",
            )
            raise
        if await _is_cancelled(context.job_id):
            return
        await _persist_usage(context.job_id, "transcription", result.usage)
        normalized_words = _validate_segment_words(
            result.words,
            segment,
            context.audio_start_time,
            context.duration_seconds,
        )
        await _write_json_atomic(
            transcript_path,
            {
                "segment_index": segment.index,
                "text": result.text,
                "words": normalized_words,
            },
        )
        completed.add(segment.index)
        stage_checkpoint = {
            **stage_checkpoint,
            "completed_segments": sorted(completed),
        }
        await _save_stage_checkpoint(
            context.job_id,
            "transcription",
            stage_checkpoint,
        )


def _word_text(words: list[dict[str, Any]]) -> str:
    result = ""
    for word in words:
        value = str(word["text"])
        if not result:
            result = value
        elif _WORD_SPACE_PUNCTUATION.match(value):
            result += value
        else:
            result += " " + value
    return result


def reconcile_overlapping_words(
    raw_segments: list[dict[str, Any]],
    segment_words: list[list[dict[str, Any]]],
    overlap_seconds: float,
    original_offset: float = 0.0,
) -> list[dict[str, Any]]:
    all_words: list[dict[str, Any]] = []
    for index, words in enumerate(segment_words):
        start = float(raw_segments[index]["start_seconds"])
        lower_boundary = (
            original_offset + start + overlap_seconds / 2
            if index > 0
            else float("-inf")
        )
        if index + 1 < len(raw_segments):
            next_start = float(raw_segments[index + 1]["start_seconds"])
            upper_boundary = original_offset + next_start + overlap_seconds / 2
        else:
            upper_boundary = float("inf")
        all_words.extend(
            word
            for word in words
            if lower_boundary <= float(word["start_time"]) < upper_boundary
        )

    all_words.sort(key=lambda word: (word["start_time"], word["end_time"]))
    deduplicated: list[dict[str, Any]] = []
    previous_word: dict[str, Any] | None = None
    for word in all_words:
        if (
            previous_word is not None
            and str(word["text"]).casefold() == str(previous_word["text"]).casefold()
            and word["start_time"] <= previous_word["end_time"]
        ):
            continue
        deduplicated.append(word)
        previous_word = word
    return deduplicated


def _make_transcript_chunks(
    words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for word in words:
        candidate = current + [word]
        should_split = bool(current) and (
            len(_word_text(candidate)) > _WORD_CHUNK_LIMIT
            or word["start_time"] - current[-1]["end_time"] >= _PAUSE_CHUNK_SECONDS
            or str(current[-1]["text"]).endswith((".", "?", "!"))
        )
        if should_split:
            chunks.append(
                {
                    "source_ref": f"transcript:{len(chunks):05d}",
                    "content": _word_text(current),
                    "start_time": current[0]["start_time"],
                    "end_time": current[-1]["end_time"],
                    "speakers": sorted(
                        {
                            word["speaker"]
                            for word in current
                            if word.get("speaker")
                        }
                    ),
                }
            )
            current = []
        current.append(word)
    if current:
        chunks.append(
            {
                "source_ref": f"transcript:{len(chunks):05d}",
                "content": _word_text(current),
                "start_time": current[0]["start_time"],
                "end_time": current[-1]["end_time"],
                "speakers": sorted(
                    {word["speaker"] for word in current if word.get("speaker")}
                ),
            }
        )
    return chunks


async def _chunk_transcript(context: JobContext) -> dict[str, Any]:
    checkpoint = await _stage_checkpoint(context.job_id, "audio_segmentation")
    raw_segments = checkpoint.get("segments", [])
    if not isinstance(raw_segments, list) or not raw_segments:
        raise MediaProcessingError(
            "audio_checkpoint_invalid",
            "The saved audio segment checkpoint is invalid.",
            retryable=False,
        )
    settings = get_settings()
    segment_words: list[list[dict[str, Any]]] = []
    for index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            raise MediaProcessingError(
                "audio_checkpoint_invalid",
                "The saved audio segment checkpoint is invalid.",
                retryable=False,
            )
        try:
            segment_index = int(raw_segment["index"])
            segment_start = float(raw_segment["start_seconds"])
            segment_duration = float(raw_segment["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaProcessingError(
                "audio_checkpoint_invalid",
                "The saved audio segment checkpoint is invalid.",
                retryable=False,
            ) from exc
        segment = AudioSegment(
            index=segment_index,
            path=context.work_dir
            / "audio"
            / f"segment-{segment_index:05d}.flac",
            start_seconds=segment_start,
            duration_seconds=segment_duration,
        )
        artifact = await _read_json(
            segment.path.with_suffix(".transcript.json")
        )
        words = artifact.get("words", [])
        if not isinstance(words, list):
            raise MediaProcessingError(
                "transcript_artifact_invalid",
                "A saved transcript segment has invalid word data.",
                retryable=False,
            )
        segment_words.append(words)

    deduplicated = reconcile_overlapping_words(
        raw_segments,
        segment_words,
        settings.audio_segment_overlap_seconds,
        context.audio_start_time,
    )

    chunks = _make_transcript_chunks(deduplicated)
    output_path = context.work_dir / "audio" / "transcript-chunks.json"
    await _write_json_atomic(
        output_path,
        {
            "video_id": str(context.video_id),
            "timeline": "original_video",
            "timestamp_precision": "word",
            "chunks": chunks,
        },
    )
    return {
        "output_path": str(output_path),
        "word_count": len(deduplicated),
        "chunk_count": len(chunks),
    }


def _scene_values(checkpoint: dict[str, Any], duration_seconds: float) -> list[DetectedScene]:
    values = checkpoint.get("scenes")
    if not isinstance(values, list):
        raise MediaProcessingError(
            "scene_checkpoint_invalid",
            "The saved scene detection checkpoint is invalid.",
            retryable=False,
        )
    scenes: list[DetectedScene] = []
    for value in values:
        if not isinstance(value, dict):
            raise MediaProcessingError(
                "scene_checkpoint_invalid",
                "The saved scene detection checkpoint is invalid.",
                retryable=False,
            )
        try:
            start = float(value["start_seconds"])
            end = float(value["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaProcessingError(
                "scene_checkpoint_invalid",
                "The saved scene detection checkpoint is invalid.",
                retryable=False,
            ) from exc
        if (
            not math.isfinite(start + end)
            or start < 0
            or end <= start
            or end > duration_seconds
        ):
            raise MediaProcessingError(
                "scene_checkpoint_invalid",
                "The saved scene detection checkpoint has invalid timestamps.",
                retryable=False,
            )
        scenes.append(DetectedScene(start_seconds=start, end_seconds=end))
    return scenes


async def _run_scene_detection(context: JobContext) -> dict[str, Any]:
    settings = get_settings()
    scenes = await detect_scenes(
        context.original_path,
        settings.scene_threshold,
        settings.scene_detection_timeout_seconds,
    )
    return {
        "scenes": [
            {
                "start_seconds": scene.start_seconds,
                "end_seconds": scene.end_seconds,
            }
            for scene in scenes
        ]
    }


async def _run_frame_extraction(context: JobContext) -> dict[str, Any]:
    settings = get_settings()
    scene_checkpoint = await _stage_checkpoint(context.job_id, "scene_detection")
    scenes = _scene_values(scene_checkpoint, context.duration_seconds)
    timestamps = select_frame_timestamps(
        scenes,
        context.duration_seconds,
        settings.frame_sampling_interval_seconds,
        settings.frame_min_spacing_seconds,
        settings.max_frames_per_video,
    )
    frames = await extract_selected_frames(
        context.original_path,
        timestamps,
        context.work_dir / "visual" / "frames",
        ffmpeg_path=settings.ffmpeg_path,
        timeout_seconds=settings.frame_extraction_timeout_seconds,
        max_dimension=settings.frame_max_dimension,
    )
    return {
        "frames": [
            {
                "index": frame.index,
                "timestamp_seconds": frame.timestamp_seconds,
            }
            for frame in frames
        ]
    }


def _frame_manifest(
    context: JobContext,
    checkpoint: dict[str, Any],
) -> list[SelectedFrame]:
    values = checkpoint.get("frames")
    if not isinstance(values, list):
        raise MediaProcessingError(
            "frame_checkpoint_invalid",
            "The saved frame extraction checkpoint is invalid.",
            retryable=False,
        )
    frames: list[SelectedFrame] = []
    seen: set[int] = set()
    frames_dir = context.work_dir / "visual" / "frames"
    for value in values:
        if not isinstance(value, dict):
            raise MediaProcessingError(
                "frame_checkpoint_invalid",
                "The saved frame extraction checkpoint is invalid.",
                retryable=False,
            )
        try:
            index = int(value["index"])
            timestamp = float(value["timestamp_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaProcessingError(
                "frame_checkpoint_invalid",
                "The saved frame extraction checkpoint is invalid.",
                retryable=False,
            ) from exc
        if (
            index < 0
            or index in seen
            or not math.isfinite(timestamp)
            or timestamp < 0
            or timestamp >= context.duration_seconds
        ):
            raise MediaProcessingError(
                "frame_checkpoint_invalid",
                "The saved frame extraction checkpoint has invalid values.",
                retryable=False,
            )
        path = frames_dir / f"frame-{index:05d}.jpg"
        if not path.is_file():
            raise MediaProcessingError(
                "frame_checkpoint_incomplete",
                "A selected frame is missing from local storage.",
                retryable=True,
                transient=True,
            )
        seen.add(index)
        frames.append(
            SelectedFrame(
                index=index,
                timestamp_seconds=timestamp,
                path=path,
            )
        )
    return frames


async def _frame_ocr_artifact(context: JobContext, index: int) -> dict[str, Any] | None:
    path = context.work_dir / "visual" / "ocr" / f"frame-{index:05d}.json"
    if not path.is_file():
        return None
    try:
        artifact = await _read_json(path)
    except MediaProcessingError:
        return None
    if artifact.get("frame_index") != index or not isinstance(
        artifact.get("text"), str
    ):
        return None
    return artifact


async def _run_ocr(context: JobContext) -> dict[str, Any]:
    frame_checkpoint = await _stage_checkpoint(context.job_id, "frame_extraction")
    frames = _frame_manifest(context, frame_checkpoint)
    checkpoint = await _stage_checkpoint(context.job_id, "ocr")
    completed_values = checkpoint.get("completed_frames", [])
    if not isinstance(completed_values, list):
        raise MediaProcessingError(
            "ocr_checkpoint_invalid",
            "The saved OCR checkpoint is invalid.",
            retryable=False,
        )
    completed = {
        value for value in completed_values if isinstance(value, int) and value >= 0
    }
    for frame in frames:
        if await _is_cancelled(context.job_id):
            return {"completed_frames": sorted(completed)}
        artifact = await _frame_ocr_artifact(context, frame.index)
        if frame.index in completed and artifact is not None:
            continue
        text_value = await asyncio.to_thread(extract_visible_text, frame.path)
        await _write_json_atomic(
            context.work_dir
            / "visual"
            / "ocr"
            / f"frame-{frame.index:05d}.json",
            {"frame_index": frame.index, "text": text_value},
        )
        completed.add(frame.index)
        checkpoint = {"completed_frames": sorted(completed)}
        await _save_stage_checkpoint(context.job_id, "ocr", checkpoint)
    return {"completed_frames": sorted(completed)}


async def _run_visual_analysis(context: JobContext) -> dict[str, Any]:
    frame_checkpoint = await _stage_checkpoint(context.job_id, "frame_extraction")
    frames = _frame_manifest(context, frame_checkpoint)
    frame_values: list[dict[str, object]] = []
    for frame in frames:
        artifact = await _frame_ocr_artifact(context, frame.index)
        if artifact is None:
            raise MediaProcessingError(
                "ocr_artifact_missing",
                "Text recognition data for a selected frame is missing.",
                retryable=True,
                transient=True,
            )
        frame_values.append(
            {
                "frame_id": str(frame.index),
                "frame_path": str(frame.path),
                "ocr_text": artifact["text"],
                "timestamp_seconds": frame.timestamp_seconds,
            }
        )

    settings = get_settings()
    retained, duplicates = near_duplicate_frames(
        frame_values,
        settings.perceptual_hash_distance,
    )
    checkpoint = await _stage_checkpoint(context.job_id, "visual_analysis")
    completed_values = checkpoint.get("completed_frames", [])
    if not isinstance(completed_values, list):
        raise MediaProcessingError(
            "visual_checkpoint_invalid",
            "The saved visual analysis checkpoint is invalid.",
            retryable=False,
        )
    completed = {
        value for value in completed_values if isinstance(value, int) and value >= 0
    }
    provider: VisionProvider | None = None
    evidence: list[dict[str, Any]] = []
    pending_evidence: list[dict[str, Any]] = []
    for frame in retained:
        frame_index = int(str(frame["frame_id"]))
        timestamp = float(frame["timestamp_seconds"])
        frame_path = Path(str(frame["frame_path"]))
        ocr_text = str(frame["ocr_text"])
        artifact_path = (
            context.work_dir
            / "visual"
            / "descriptions"
            / f"frame-{frame_index:05d}.json"
        )
        artifact: dict[str, Any] | None = None
        if artifact_path.is_file():
            try:
                cached = await _read_json(artifact_path)
            except MediaProcessingError:
                cached = {}
            if (
                cached.get("frame_index") == frame_index
                and cached.get("timestamp_seconds") == timestamp
                and cached.get("ocr_text") == ocr_text
                and isinstance(cached.get("description"), str)
            ):
                artifact = cached
        if artifact is None:
            if await _is_cancelled(context.job_id):
                return {"completed_frames": sorted(completed)}
            if provider is None:
                provider = create_vision_provider()
            try:
                result = await provider.describe_frame(frame_path, ocr_text)
            except ProviderError as exc:
                await _persist_provider_error(
                    context.job_id,
                    "visual_analysis",
                    frame_path,
                    exc,
                    model_id=VISION_MODEL_ID,
                    operation="vision",
                )
                raise
            await _persist_usage(context.job_id, "visual_analysis", result.usage)
            artifact = {
                "frame_index": frame_index,
                "timestamp_seconds": timestamp,
                "frame_path": str(frame_path),
                "ocr_text": ocr_text,
                "description": result.description,
            }
            await _write_json_atomic(artifact_path, artifact)
        completed.add(frame_index)
        await _save_stage_checkpoint(
            context.job_id,
            "visual_analysis",
            {"completed_frames": sorted(completed)},
        )
        description = str(artifact["description"])
        searchable_content = description
        if ocr_text:
            searchable_content = f"{description}\nVisible text: {ocr_text}"
        evidence.append(
            {
                "source_ref": f"visual:frame:{frame_index:05d}",
                "content": searchable_content,
                "start_time": timestamp,
                "end_time": timestamp,
                "metadata": {
                    "frame_index": frame_index,
                    "frame_path": str(frame_path),
                    "ocr_text": ocr_text,
                    "visual_description": description,
                    "duplicate_of": None,
                },
            }
        )
        pending_evidence.append(evidence[-1])
        if len(pending_evidence) >= settings.embedding_batch_size:
            await _index_evidence_items(
                context,
                ChunkType.VISUAL,
                pending_evidence,
            )
            pending_evidence.clear()

    if pending_evidence:
        await _index_evidence_items(
            context,
            ChunkType.VISUAL,
            pending_evidence,
        )

    for duplicate_index, source_index in duplicates.items():
        logger.debug(
            "Near-duplicate video frame omitted",
            extra={
                "video_id": str(context.video_id),
                "frame_index": duplicate_index,
                "retained_frame_index": source_index,
            },
        )
    output_path = context.work_dir / "visual" / "visual-evidence.json"
    await _write_json_atomic(
        output_path,
        {
            "video_id": str(context.video_id),
            "timeline": "original_video",
            "sampling_policy": {
                "version": "scene-centers-plus-configured-interval-v1",
                "interval_seconds": settings.frame_sampling_interval_seconds,
                "minimum_spacing_seconds": settings.frame_min_spacing_seconds,
                "maximum_frames": settings.max_frames_per_video,
                "maximum_dimension": settings.frame_max_dimension,
                "perceptual_hash_distance": settings.perceptual_hash_distance,
            },
            "duplicate_count": len(duplicates),
            "evidence": evidence,
        },
    )
    return {
        "output_path": str(output_path),
        "evidence_count": len(evidence),
        "duplicate_count": len(duplicates),
        "completed_frames": sorted(completed),
    }


async def _run_visual_branch(context: JobContext) -> None:
    scene_checkpoint = await _stage_checkpoint(context.job_id, "scene_detection")
    try:
        _scene_values(scene_checkpoint, context.duration_seconds)
    except MediaProcessingError:
        await _reset_stage(context.job_id, "scene_detection")
        await _reset_stage(context.job_id, "frame_extraction")
        await _reset_stage(context.job_id, "ocr")
        await _reset_stage(context.job_id, "visual_analysis")

    await _run_branch_stage(
        context,
        "scene_detection",
        lambda: _run_scene_detection(context),
    )
    if await _is_cancelled(context.job_id):
        return
    frame_checkpoint = await _stage_checkpoint(context.job_id, "frame_extraction")
    try:
        _frame_manifest(context, frame_checkpoint)
    except MediaProcessingError:
        await _reset_stage(context.job_id, "frame_extraction")
        await _reset_stage(context.job_id, "ocr")
        await _reset_stage(context.job_id, "visual_analysis")

    await _run_branch_stage(
        context,
        "frame_extraction",
        lambda: _run_frame_extraction(context),
    )
    if await _is_cancelled(context.job_id):
        return
    frame_checkpoint = await _stage_checkpoint(context.job_id, "frame_extraction")
    try:
        frames = _frame_manifest(context, frame_checkpoint)
    except MediaProcessingError as error:
        raise BranchProcessingError("frame_extraction", error) from error
    ocr_checkpoint = await _stage_checkpoint(context.job_id, "ocr")
    completed_ocr = ocr_checkpoint.get("completed_frames", [])
    if (
        not isinstance(completed_ocr, list)
        or {
            value for value in completed_ocr if isinstance(value, int)
        }
        != {frame.index for frame in frames}
        or any(
            not (
                context.work_dir
                / "visual"
                / "ocr"
                / f"frame-{frame.index:05d}.json"
            ).is_file()
            for frame in frames
        )
    ):
        await _reset_stage(context.job_id, "ocr")
        await _reset_stage(context.job_id, "visual_analysis")
    await _run_branch_stage(context, "ocr", lambda: _run_ocr(context))
    if await _is_cancelled(context.job_id):
        return
    analysis_checkpoint = await _stage_checkpoint(
        context.job_id,
        "visual_analysis",
    )
    output_path = analysis_checkpoint.get("output_path")
    expected_output = context.work_dir / "visual" / "visual-evidence.json"
    reset_analysis_stage = (
        not isinstance(output_path, str)
        or Path(output_path) != expected_output
        or not expected_output.is_file()
    )
    if not reset_analysis_stage:
        try:
            cached_evidence = await _read_json(expected_output)
            reset_analysis_stage = not isinstance(
                cached_evidence.get("evidence"),
                list,
            )
        except MediaProcessingError:
            reset_analysis_stage = True
    if reset_analysis_stage:
        await _reset_stage(context.job_id, "visual_analysis")
    await _run_branch_stage(
        context,
        "visual_analysis",
        lambda: _run_visual_analysis(context),
    )
    try:
        evidence_payload = await _read_json(expected_output)
    except MediaProcessingError as error:
        raise BranchProcessingError("visual_analysis", error) from error
    evidence = evidence_payload.get("evidence")
    if not isinstance(evidence, list):
        raise BranchProcessingError(
            "indexing",
            MediaProcessingError(
                "visual_evidence_invalid",
                "The saved visual evidence artifact is incomplete.",
                retryable=False,
            ),
        )
    await _index_evidence_items(
        context,
        ChunkType.VISUAL,
        evidence,
        prune=True,
    )


async def _run_stage(
    context: JobContext,
    stage_name: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> bool:
    if not await _start_stage(context.job_id, stage_name):
        return False
    checkpoint = await operation()
    return await _complete_stage(context.job_id, stage_name, checkpoint)


class BranchProcessingError(Exception):
    def __init__(
        self,
        stage_name: str,
        error: ProviderError | MediaProcessingError,
    ) -> None:
        super().__init__(error.user_message)
        self.stage_name = stage_name
        self.error = error


async def _index_evidence_items(
    context: JobContext,
    chunk_type: ChunkType,
    evidence: list[dict[str, Any]],
    *,
    prune: bool = False,
) -> None:
    try:
        await index_evidence(
            context.job_id,
            context.video_id,
            chunk_type,
            evidence,
        )
        if prune:
            await prune_stale_evidence(context.video_id, chunk_type, evidence)
    except ProviderError as error:
        raise BranchProcessingError("embeddings", error) from error
    except MediaProcessingError as error:
        raise BranchProcessingError("indexing", error) from error


async def _run_branch_stage(
    context: JobContext,
    stage_name: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> bool:
    try:
        return await _run_stage(context, stage_name, operation)
    except (ProviderError, MediaProcessingError) as error:
        raise BranchProcessingError(stage_name, error) from error


async def _skip_stage(job_id: UUID, stage_name: str) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            stage = await session.scalar(
                select(ProcessingStage)
                .where(
                    ProcessingStage.job_id == job_id,
                    ProcessingStage.name == stage_name,
                )
                .with_for_update()
            )
            if stage is not None and stage.status == StageStatus.PENDING:
                stage.status = StageStatus.SKIPPED
                stage.finished_at = now


async def _run_audio_branch(context: JobContext) -> None:
    audio_stages = (
        "audio_extraction",
        "audio_segmentation",
        "transcription",
        "transcript_chunking",
    )
    if not context.has_audio:
        for stage_name in audio_stages:
            await _skip_stage(context.job_id, stage_name)
        return

    audio_path = context.work_dir / "audio" / "audio.flac"
    if not audio_path.is_file():
        await _reset_stage(context.job_id, "audio_extraction")

    async def extract() -> dict[str, Any]:
        path, duration = await _prepare_audio(context)
        return {"audio_path": str(path), "duration_seconds": duration}

    await _run_branch_stage(context, "audio_extraction", extract)
    if await _is_cancelled(context.job_id):
        return
    checkpoint = await _stage_checkpoint(context.job_id, "audio_extraction")
    try:
        audio_path = Path(checkpoint["audio_path"])
        audio_duration = float(checkpoint["duration_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BranchProcessingError(
            "audio_extraction",
            MediaProcessingError(
                "audio_checkpoint_invalid",
                "The saved audio extraction checkpoint is invalid.",
                retryable=False,
            ),
        ) from exc
    if (
        not audio_path.is_file()
        or not math.isfinite(audio_duration)
        or audio_duration <= 0
    ):
        raise BranchProcessingError(
            "audio_extraction",
            MediaProcessingError(
                "audio_checkpoint_invalid",
                "The extracted audio file or its duration is invalid.",
                retryable=False,
            ),
        )

    segment_checkpoint = await _stage_checkpoint(
        context.job_id,
        "audio_segmentation",
    )
    saved_segments = segment_checkpoint.get("segments", [])
    if not isinstance(saved_segments, list) or not saved_segments or any(
        not _segment_artifact_exists(context, item, "flac")
        for item in saved_segments
    ):
        await _reset_stage(context.job_id, "audio_segmentation")

    async def segment() -> dict[str, Any]:
        segments = await _create_audio_segments(
            context,
            audio_path,
            audio_duration,
        )
        return {
            "segments": [
                {
                    "index": item.index,
                    "path": str(item.path),
                    "start_seconds": item.start_seconds,
                    "duration_seconds": item.duration_seconds,
                }
                for item in segments
            ]
        }

    await _run_branch_stage(context, "audio_segmentation", segment)
    if await _is_cancelled(context.job_id):
        return

    segmentation_checkpoint = await _stage_checkpoint(
        context.job_id,
        "audio_segmentation",
    )
    raw_segments = segmentation_checkpoint.get("segments", [])
    if not isinstance(raw_segments, list) or any(
        not _segment_artifact_exists(context, item, "transcript.json")
        for item in raw_segments
    ):
        await _reset_stage(context.job_id, "transcription")

    async def transcribe() -> dict[str, Any]:
        await _transcribe_segments(context)
        return await _stage_checkpoint(context.job_id, "transcription")

    await _run_branch_stage(context, "transcription", transcribe)
    if await _is_cancelled(context.job_id):
        return

    chunk_checkpoint = await _stage_checkpoint(context.job_id, "transcript_chunking")
    chunk_path = chunk_checkpoint.get("output_path")
    reset_chunk_stage = not isinstance(chunk_path, str) or not Path(chunk_path).is_file()
    if not reset_chunk_stage:
        try:
            cached_chunks = await _read_json(Path(chunk_path))
            reset_chunk_stage = not isinstance(cached_chunks.get("chunks"), list)
        except MediaProcessingError:
            reset_chunk_stage = True
    if reset_chunk_stage:
        await _reset_stage(context.job_id, "transcript_chunking")
    await _run_branch_stage(
        context,
        "transcript_chunking",
        lambda: _chunk_transcript(context),
    )
    try:
        chunk_payload = await _read_json(
            context.work_dir / "audio" / "transcript-chunks.json"
        )
    except MediaProcessingError as error:
        raise BranchProcessingError("transcript_chunking", error) from error
    chunks = chunk_payload.get("chunks")
    if not isinstance(chunks, list):
        raise BranchProcessingError(
            "transcript_chunking",
            MediaProcessingError(
                "transcript_artifact_invalid",
                "The saved transcript chunks are incomplete.",
                retryable=False,
            ),
        )
    await _index_evidence_items(
        context,
        ChunkType.TRANSCRIPT,
        chunks,
        prune=True,
    )


async def _requeue_transient_failure(
    job_id: UUID,
    stage_name: str,
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
                stage.status = StageStatus.PENDING
                stage.last_error = {
                    "code": error_code,
                    "user_message": user_message,
                    "retryable": True,
                }
            job.status = LifecycleStatus.QUEUED
            job.current_stage = stage_name
            job.last_error = {
                "code": error_code,
                "user_message": user_message,
                "retryable": True,
            }
            job.finished_at = None
            job.lease_expires_at = None
            job.last_enqueued_at = now
            asset = await session.get(VideoAsset, job.video_id)
            if asset is not None:
                asset.status = LifecycleStatus.QUEUED


def _retry_delay(retry_count: int, retry_after: datetime | None) -> int:
    now = datetime.now(timezone.utc)
    if retry_after is not None:
        return max(1, math.ceil((retry_after - now).total_seconds()))
    base = min(300, 2 ** min(retry_count, 8))
    return max(1, base + random.SystemRandom().randint(0, max(1, base // 4)))


async def _handle_retryable_error(
    job_id: UUID,
    stage_name: str,
    error: ProviderError | MediaProcessingError,
    retry_count: int,
) -> None:
    settings = get_settings()
    if isinstance(error, ProviderError) and error.quota:
        await pause_job_for_quota(
            job_id,
            stage_name,
            error.retry_after,
            error.code,
            error.user_message,
        )
        return
    async with SessionLocal() as session:
        attempt_count = await session.scalar(
            select(ProcessingJob.attempt_count).where(
                ProcessingJob.id == job_id
            )
        )
    if (
        error.transient
        and attempt_count is not None
        and attempt_count < settings.job_max_attempts
    ):
        await _requeue_transient_failure(
            job_id,
            stage_name,
            error.code,
            error.user_message,
        )
        raise RetryableJobError(
            error.user_message,
            _retry_delay(
                retry_count,
                error.retry_after if isinstance(error, ProviderError) else None,
            ),
        ) from error
    await mark_job_failed(
        job_id,
        stage_name,
        error.code,
        error.user_message,
        retryable=error.retryable,
    )


async def _record_branch_failures(
    job_id: UUID,
    failures: list[BranchProcessingError],
) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        async with session.begin():
            for failure in failures:
                stage = await session.scalar(
                    select(ProcessingStage)
                    .where(
                        ProcessingStage.job_id == job_id,
                        ProcessingStage.name == failure.stage_name,
                    )
                    .with_for_update()
                )
                if stage is None:
                    continue
                stage.status = StageStatus.FAILED
                stage.finished_at = now
                stage.last_error = {
                    "code": failure.error.code,
                    "user_message": failure.error.user_message,
                    "retryable": failure.error.retryable,
                }
            fallback_error = failures[0].error
            active_stages = (
                await session.scalars(
                    select(ProcessingStage)
                    .where(
                        ProcessingStage.job_id == job_id,
                        ProcessingStage.status == StageStatus.RUNNING,
                    )
                    .with_for_update()
                )
            ).all()
            for stage in active_stages:
                stage.status = StageStatus.FAILED
                stage.finished_at = now
                stage.last_error = {
                    "code": fallback_error.code,
                    "user_message": fallback_error.user_message,
                    "retryable": fallback_error.retryable,
                }


async def process_ingestion_job(job_id: UUID, retry_count: int = 0) -> None:
    lock_connection = await _try_job_lock(job_id)
    if lock_connection is None:
        return

    stop_heartbeat = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    try:
        try:
            context = await _claim_job(job_id)
        except (ProviderError, MediaProcessingError) as exc:
            await _handle_retryable_error(
                job_id,
                "media_preparation",
                exc,
                retry_count,
            )
            return
        if context is None:
            return
        heartbeat_task = asyncio.create_task(_heartbeat(job_id, stop_heartbeat))

        try:
            if not context.work_dir.is_dir():
                await _reset_stage(job_id, "media_preparation")
            await _run_branch_stage(
                context,
                "media_preparation",
                lambda: _run_media_preparation(context),
            )
            if await _is_cancelled(job_id):
                return

            await _start_stage(job_id, "embeddings")
            await _start_stage(job_id, "indexing")
            results = await asyncio.gather(
                _run_audio_branch(context),
                _run_visual_branch(context),
                return_exceptions=True,
            )
            failures = [
                result
                for result in results
                if isinstance(result, BranchProcessingError)
            ]
            unexpected = [
                result
                for result in results
                if isinstance(result, Exception)
                and not isinstance(result, BranchProcessingError)
            ]
            if unexpected:
                raise unexpected[0]
            if failures:
                await _record_branch_failures(job_id, failures)
                selected = next(
                    (
                        failure
                        for failure in failures
                        if isinstance(failure.error, ProviderError)
                        and failure.error.quota
                    ),
                    failures[0],
                )
                await _handle_retryable_error(
                    job_id,
                    selected.stage_name,
                    selected.error,
                    retry_count,
                )
                return
            if await _is_cancelled(job_id):
                return
            settings = get_settings()
            await _complete_stage(
                job_id,
                "embeddings",
                {
                    "model_id": settings.embedding_model_id,
                    "model_version": settings.embedding_model_version,
                    "dimension": settings.embedding_dimension,
                },
            )
            await _complete_stage(
                job_id,
                "indexing",
                {"database": "postgresql", "vector_store": "pgvector"},
            )
            try:
                await _set_lifecycle_status(job_id, LifecycleStatus.INDEXING)
            except MediaProcessingError as error:
                raise BranchProcessingError("indexing", error) from error
            try:
                await _set_lifecycle_status(job_id, LifecycleStatus.VERIFYING)
            except MediaProcessingError as error:
                raise BranchProcessingError("verification", error) from error
            await _run_branch_stage(
                context,
                "verification",
                lambda: _verify_index_artifacts(context),
            )
            if await _is_cancelled(job_id):
                return
            try:
                await _mark_job_ready(job_id)
            except MediaProcessingError as error:
                raise BranchProcessingError("verification", error) from error
        except BranchProcessingError as exc:
            await _handle_retryable_error(
                job_id,
                exc.stage_name,
                exc.error,
                retry_count,
            )
        except (ProviderError, MediaProcessingError) as exc:
            await _handle_retryable_error(
                job_id,
                "media_preparation",
                exc,
                retry_count,
            )
    finally:
        if heartbeat_task is not None:
            stop_heartbeat.set()
            await heartbeat_task
        await _release_job_lock(lock_connection, job_id)


async def reconcile_processing_jobs() -> int:
    from app.services.jobs.dispatch import reconcile_jobs

    return await reconcile_jobs()
