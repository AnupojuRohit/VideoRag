import argparse
import asyncio
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectedScene:
    start_seconds: float
    end_seconds: float


def _detect_scenes_sync(path: Path, threshold: float) -> list[DetectedScene]:
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(
        ContentDetector(
            threshold=threshold,
            min_scene_len=max(1, round(video.frame_rate)),
        )
    )
    manager.detect_scenes(video=video)
    return [
        DetectedScene(
            start_seconds=start.get_seconds(),
            end_seconds=end.get_seconds(),
        )
        for start, end in manager.get_scene_list()
    ]


def _child_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--threshold", required=True, type=float)
    args = parser.parse_args()
    scenes = _detect_scenes_sync(Path(args.input), args.threshold)
    print(
        json.dumps(
            [
                {
                    "start_seconds": scene.start_seconds,
                    "end_seconds": scene.end_seconds,
                }
                for scene in scenes
            ]
        )
    )
    return 0


async def detect_scenes(
    path: Path,
    threshold: float,
    timeout_seconds: int,
) -> list[DetectedScene]:
    from app.services.media.audio import MediaProcessingError

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--input",
            str(path),
            "--threshold",
            str(threshold),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise MediaProcessingError(
            "scene_detection_timeout",
            "Scene detection exceeded the configured time limit.",
            retryable=True,
            transient=True,
        ) from exc
    except OSError as exc:
        logger.error(
            "Scene detection process could not be started",
            extra={"error_type": type(exc).__name__},
        )
        raise MediaProcessingError(
            "scene_detection_unavailable",
            "The local scene detector could not be started.",
            retryable=True,
            transient=True,
        ) from exc

    if process.returncode != 0:
        raise MediaProcessingError(
            "scene_detection_failed",
            "The video scenes could not be detected.",
            retryable=True,
            transient=True,
        )
    try:
        values: Any = json.loads(stdout)
        if not isinstance(values, list):
            raise ValueError("scene response is not a list")
        if any(not isinstance(value, dict) for value in values):
            raise ValueError("scene response contains a non-object item")
        scenes = [
            DetectedScene(
                start_seconds=float(value["start_seconds"]),
                end_seconds=float(value["end_seconds"]),
            )
            for value in values
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaProcessingError(
            "scene_detection_response_invalid",
            "The local scene detector returned invalid scene data.",
            retryable=False,
        ) from exc
    if any(
        not math.isfinite(scene.start_seconds + scene.end_seconds)
        or scene.start_seconds < 0
        or scene.end_seconds <= scene.start_seconds
        for scene in scenes
    ):
        raise MediaProcessingError(
            "scene_detection_response_invalid",
            "The local scene detector returned invalid scene timestamps.",
            retryable=False,
        )
    return scenes


if __name__ == "__main__":
    raise SystemExit(_child_main())
