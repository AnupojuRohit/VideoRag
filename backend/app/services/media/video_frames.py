import asyncio
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.core.config import get_settings
from app.services.media.audio import MediaProcessingError
from app.services.media.scene_detection import DetectedScene

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelectedFrame:
    index: int
    timestamp_seconds: float
    path: Path


def _evenly_spaced(values: list[float], count: int) -> list[float]:
    if count <= 0 or not values:
        return []
    if len(values) <= count:
        return values
    if count == 1:
        return [values[len(values) // 2]]
    indices = [
        round(index * (len(values) - 1) / (count - 1))
        for index in range(count)
    ]
    return [values[index] for index in indices]


def select_frame_timestamps(
    scenes: list[DetectedScene],
    duration_seconds: float,
    interval_seconds: float,
    min_spacing_seconds: float,
    max_frames: int,
) -> list[float]:
    if (
        not math.isfinite(duration_seconds)
        or duration_seconds <= 0
        or not math.isfinite(interval_seconds)
        or interval_seconds <= 0
        or not math.isfinite(min_spacing_seconds)
        or min_spacing_seconds < 0
        or max_frames <= 0
    ):
        raise ValueError("Invalid frame sampling configuration")

    scene_centers = [
        (scene.start_seconds + scene.end_seconds) / 2
        for scene in scenes
        if 0 <= scene.start_seconds < scene.end_seconds <= duration_seconds
    ]
    interval_samples: list[float] = []
    timestamp = interval_seconds / 2
    while timestamp < duration_seconds:
        interval_samples.append(timestamp)
        timestamp += interval_seconds
    if not scene_centers and not interval_samples:
        interval_samples.append(duration_seconds / 2)

    prioritized = sorted(
        [(value, 0) for value in scene_centers]
        + [(value, 1) for value in interval_samples],
        key=lambda item: (item[1], item[0]),
    )
    accepted: list[tuple[float, int]] = []
    for candidate in prioritized:
        if all(
            abs(candidate[0] - chosen[0]) >= min_spacing_seconds
            for chosen in accepted
        ):
            accepted.append(candidate)

    if len(accepted) > max_frames:
        scene_values = [value for value, priority in accepted if priority == 0]
        interval_values = [value for value, priority in accepted if priority == 1]
        chosen_scenes = _evenly_spaced(scene_values, max_frames)
        remaining = max_frames - len(chosen_scenes)
        chosen_intervals = _evenly_spaced(interval_values, remaining)
        selected = chosen_scenes + chosen_intervals
    else:
        selected = [value for value, _ in accepted]
    return sorted(set(selected))


async def _run_ffmpeg(command: list[str], timeout_seconds: int) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise MediaProcessingError(
            "frame_extraction_timeout",
            "Frame extraction exceeded the configured time limit.",
            retryable=True,
            transient=True,
        ) from exc
    except OSError as exc:
        logger.error(
            "FFmpeg frame extraction could not be started",
            extra={"error_type": type(exc).__name__},
        )
        raise MediaProcessingError(
            "frame_extraction_unavailable",
            "The local frame extractor could not be started.",
            retryable=True,
            transient=True,
        ) from exc
    if process.returncode != 0:
        raise MediaProcessingError(
            "frame_extraction_failed",
            "A selected video frame could not be extracted.",
            retryable=True,
            transient=True,
        )


async def extract_selected_frames(
    source_path: Path,
    timestamps: list[float],
    output_dir: Path,
    *,
    ffmpeg_path: str,
    timeout_seconds: int,
    max_dimension: int,
) -> list[SelectedFrame]:
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    frames: list[SelectedFrame] = []
    for index, timestamp in enumerate(timestamps):
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MediaProcessingError(
                "frame_timestamp_invalid",
                "A selected frame has an invalid timestamp.",
                retryable=False,
            )
        path = output_dir / f"frame-{index:05d}.jpg"
        await _run_ffmpeg(
            [
                ffmpeg_path,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-ss",
                f"{timestamp:.6f}",
                "-threads",
                "1",
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                f"scale={max_dimension}:{max_dimension}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-threads",
                "1",
                "-q:v",
                "3",
                "-y",
                str(path),
            ],
            timeout_seconds,
        )
        try:
            frame_size = path.stat().st_size
        except OSError as exc:
            raise MediaProcessingError(
                "frame_extraction_failed",
                "A selected video frame could not be saved.",
                retryable=True,
                transient=True,
            ) from exc
        if not path.is_file() or frame_size == 0:
            raise MediaProcessingError(
                "frame_extraction_failed",
                "A selected video frame could not be saved.",
                retryable=True,
                transient=True,
            )
        frames.append(
            SelectedFrame(
                index=index,
                timestamp_seconds=timestamp,
                path=path,
            )
        )
    return frames


def difference_hash(path: Path) -> int:
    try:
        with Image.open(path) as source:
            pixels = list(source.convert("L").resize((9, 8)).tobytes())
    except (OSError, UnidentifiedImageError) as exc:
        raise MediaProcessingError(
            "frame_image_invalid",
            "An extracted video frame is not a valid image.",
            retryable=False,
        ) from exc
    result = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            result = (result << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return result


def near_duplicate_frames(
    frames: list[dict[str, object]],
    max_hamming_distance: int,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    kept: list[dict[str, object]] = []
    hashes: list[tuple[int, str, str]] = []
    duplicates: dict[str, str] = {}
    for frame in frames:
        frame_id = str(frame["frame_id"])
        frame_hash = difference_hash(Path(str(frame["frame_path"])))
        ocr_text = " ".join(str(frame.get("ocr_text", "")).casefold().split())
        duplicate_of: str | None = None
        for prior_hash, prior_id, prior_ocr in hashes:
            if (
                ocr_text == prior_ocr
                and (frame_hash ^ prior_hash).bit_count() <= max_hamming_distance
            ):
                duplicate_of = prior_id
                break
        if duplicate_of is None:
            kept.append(frame)
            hashes.append((frame_hash, frame_id, ocr_text))
        else:
            duplicates[frame_id] = duplicate_of
    return kept, duplicates
