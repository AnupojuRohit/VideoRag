import asyncio
import json
import logging
import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.errors import APIError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float
    video_codec: str
    audio_codec: str | None
    audio_start_time: float | None
    width: int
    height: int
    frame_rate: float | None


def _frame_rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


async def inspect_mp4(path: Path) -> MediaInfo:
    settings = get_settings()
    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-probesize",
        "32M",
        "-analyzeduration",
        "10000000",
        "-max_alloc",
        "100000000",
        "-show_entries",
        "format=format_name,duration:stream=codec_type,codec_name,width,height,avg_frame_rate,start_time",
        "-of",
        "json",
        str(path),
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        logger.error(
            "FFprobe executable is unavailable",
            extra={"error_type": type(exc).__name__},
        )
        raise APIError(
            503,
            "media_inspector_unavailable",
            "Video inspection is temporarily unavailable.",
        ) from exc

    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.ffprobe_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise APIError(
            422,
            "media_inspection_timeout",
            "The video could not be inspected within the allowed time.",
        ) from exc

    if process.returncode != 0:
        raise APIError(
            422,
            "invalid_media",
            "The uploaded file is not a readable MP4 video.",
        )

    try:
        probe: dict[str, Any] = json.loads(stdout)
        streams = probe.get("streams", [])
        format_info = probe.get("format", {})
        format_names = set(format_info.get("format_name", "").split(","))
        duration = float(format_info["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise APIError(
            422,
            "invalid_media",
            "The uploaded file is missing required video metadata.",
        ) from exc

    if not {"mp4", "mov"} & format_names:
        raise APIError(
            415,
            "unsupported_container",
            "Only MP4 video containers are supported.",
        )
    if not math.isfinite(duration) or duration <= 0:
        raise APIError(422, "invalid_duration", "The video duration is invalid.")
    if duration > settings.max_video_duration_seconds:
        raise APIError(
            413,
            "video_too_long",
            f"Videos must be {settings.max_video_duration_seconds} seconds or shorter.",
        )

    video_streams = [
        stream for stream in streams if stream.get("codec_type") == "video"
    ]
    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    if not video_streams:
        raise APIError(422, "video_stream_missing", "The file contains no video stream.")
    if any(stream.get("codec_name") != "h264" for stream in video_streams):
        raise APIError(
            415,
            "unsupported_video_codec",
            "Only H.264 video streams are currently supported.",
        )
    if any(stream.get("codec_name") != "aac" for stream in audio_streams):
        raise APIError(
            415,
            "unsupported_audio_codec",
            "Only AAC audio streams are currently supported.",
        )

    video = video_streams[0]
    audio_start_time = 0.0
    if audio_streams:
        try:
            parsed_audio_start = float(audio_streams[0].get("start_time", 0))
        except (TypeError, ValueError) as exc:
            raise APIError(
                422,
                "invalid_audio_timeline",
                "The audio stream has invalid timeline metadata.",
            ) from exc
        if not math.isfinite(parsed_audio_start):
            raise APIError(
                422,
                "invalid_audio_timeline",
                "The audio stream has invalid timeline metadata.",
            )
        audio_start_time = max(0.0, parsed_audio_start)
    try:
        width = int(video["width"])
        height = int(video["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise APIError(
            422,
            "invalid_video_dimensions",
            "The video stream has invalid dimensions.",
        ) from exc
    if width <= 0 or height <= 0:
        raise APIError(
            422,
            "invalid_video_dimensions",
            "The video stream has invalid dimensions.",
        )

    return MediaInfo(
        duration_seconds=duration,
        video_codec="h264",
        audio_codec="aac" if audio_streams else None,
        audio_start_time=audio_start_time if audio_streams else None,
        width=width,
        height=height,
        frame_rate=_frame_rate(video.get("avg_frame_rate")),
    )
