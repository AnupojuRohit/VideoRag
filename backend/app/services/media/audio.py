import asyncio
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class MediaProcessingError(Exception):
    def __init__(
        self,
        code: str,
        user_message: str,
        *,
        retryable: bool,
        transient: bool = False,
    ) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.retryable = retryable
        self.transient = transient


@dataclass(frozen=True)
class AudioSegment:
    index: int
    path: Path
    start_seconds: float
    duration_seconds: float


def build_audio_segments(
    audio_path: Path,
    duration_seconds: float,
    segment_seconds: float,
    overlap_seconds: float,
) -> list[AudioSegment]:
    if (
        not math.isfinite(duration_seconds)
        or duration_seconds <= 0
        or segment_seconds <= 0
        or overlap_seconds < 0
        or overlap_seconds >= segment_seconds
    ):
        raise ValueError("Invalid audio segment parameters")

    stride = segment_seconds - overlap_seconds
    segments: list[AudioSegment] = []
    start = 0.0
    while start < duration_seconds:
        length = min(segment_seconds, duration_seconds - start)
        segments.append(
            AudioSegment(
                index=len(segments),
                path=audio_path.with_name(f"segment-{len(segments):05d}.flac"),
                start_seconds=start,
                duration_seconds=length,
            )
        )
        if start + length >= duration_seconds:
            break
        start += stride
    return segments


async def _run_media_command(
    command: list[str],
    timeout_seconds: int,
    operation: str,
) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        logger.error(
            "FFmpeg executable is unavailable",
            extra={"error_type": type(exc).__name__},
        )
        raise MediaProcessingError(
            "ffmpeg_unavailable",
            "Audio processing is unavailable because FFmpeg is not installed.",
            retryable=True,
        ) from exc
    except OSError as exc:
        logger.error(
            "Unable to start an FFmpeg process",
            extra={"error_type": type(exc).__name__},
        )
        raise MediaProcessingError(
            "ffmpeg_start_failed",
            "The local audio processor could not be started.",
            retryable=True,
            transient=True,
        ) from exc

    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise MediaProcessingError(
            "audio_processing_timeout",
            "Audio processing exceeded the configured time limit.",
            retryable=True,
            transient=True,
        ) from exc
    if process.returncode != 0:
        raise MediaProcessingError(
            f"{operation}_failed",
            "The audio track could not be prepared for transcription.",
            retryable=False,
        )
    return stdout


async def extract_audio_track(source_path: Path, destination: Path) -> None:
    settings = get_settings()
    await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
    command = [
        settings.ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source_path),
        "-af",
        "asetpts=PTS-STARTPTS",
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "flac",
        "-compression_level",
        "5",
        "-threads",
        "1",
        "-y",
        str(destination),
    ]
    await _run_media_command(
        command,
        settings.ffmpeg_timeout_seconds,
        "audio_extraction",
    )


async def inspect_audio_duration(audio_path: Path) -> float:
    settings = get_settings()
    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(audio_path),
    ]
    output = await _run_media_command(
        command,
        settings.ffprobe_timeout_seconds,
        "audio_inspection",
    )
    try:
        probe: dict[str, Any] = json.loads(output)
        duration = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaProcessingError(
            "audio_metadata_invalid",
            "The extracted audio is missing valid duration metadata.",
            retryable=False,
        ) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaProcessingError(
            "audio_metadata_invalid",
            "The extracted audio has an invalid duration.",
            retryable=False,
        )
    return duration


async def extract_audio_segment(
    source_path: Path,
    segment: AudioSegment,
) -> None:
    settings = get_settings()
    await asyncio.to_thread(segment.path.parent.mkdir, parents=True, exist_ok=True)
    command = [
        settings.ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-ss",
        f"{segment.start_seconds:.6f}",
        "-i",
        str(source_path),
        "-t",
        f"{segment.duration_seconds:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "flac",
        "-compression_level",
        "5",
        "-threads",
        "1",
        "-y",
        str(segment.path),
    ]
    await _run_media_command(
        command,
        settings.ffmpeg_timeout_seconds,
        "audio_segmentation",
    )
