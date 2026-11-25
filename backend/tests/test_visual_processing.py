import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from app.providers.errors import VisionProviderError
from app.providers.gemini_vision import (
    API_REVISION,
    MODEL_ID,
    GeminiVisionProvider,
)
from app.services.media.audio import MediaProcessingError
from app.services.media.scene_detection import DetectedScene
from app.services.media.video_frames import (
    _run_ffmpeg,
    extract_selected_frames,
    near_duplicate_frames,
    select_frame_timestamps,
)


def _write_pattern(path: Path, invert: bool = False) -> None:
    image = Image.new("L", (64, 64), color=255 if invert else 0)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (0, 0, 31, 63),
        fill=0 if invert else 255,
    )
    image.save(path, format="JPEG")


def test_frame_sampling_prioritizes_scenes_and_respects_limits() -> None:
    scenes = [
        DetectedScene(start_seconds=10, end_seconds=14),
        DetectedScene(start_seconds=40, end_seconds=44),
    ]

    timestamps = select_frame_timestamps(
        scenes,
        duration_seconds=60,
        interval_seconds=10,
        min_spacing_seconds=3,
        max_frames=3,
    )

    assert len(timestamps) <= 3
    assert 12 in timestamps
    assert 42 in timestamps
    assert all(
        later - earlier >= 3
        for earlier, later in zip(timestamps, timestamps[1:])
    )


def test_frame_sampling_handles_short_video_and_rejects_nonfinite_config() -> None:
    assert select_frame_timestamps([], 2, 15, 1, 10) == [1]
    with pytest.raises(ValueError):
        select_frame_timestamps([], 60, float("inf"), 1, 10)


def test_near_duplicate_filter_preserves_ocr_changes(tmp_path: Path) -> None:
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    third = tmp_path / "third.jpg"
    _write_pattern(first)
    _write_pattern(second)
    _write_pattern(third, invert=True)
    frames = [
        {"frame_id": "1", "frame_path": str(first), "ocr_text": "Slide A"},
        {"frame_id": "2", "frame_path": str(second), "ocr_text": "Slide B"},
        {"frame_id": "3", "frame_path": str(third), "ocr_text": "Slide A"},
    ]

    kept, duplicates = near_duplicate_frames(frames, max_hamming_distance=4)

    assert [frame["frame_id"] for frame in kept] == ["1", "2", "3"]
    assert duplicates == {}


def test_near_duplicate_filter_drops_similar_frame_with_same_ocr(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    _write_pattern(first)
    _write_pattern(second)

    kept, duplicates = near_duplicate_frames(
        [
            {"frame_id": "1", "frame_path": str(first), "ocr_text": "Same"},
            {"frame_id": "2", "frame_path": str(second), "ocr_text": " same "},
        ],
        max_hamming_distance=4,
    )

    assert [frame["frame_id"] for frame in kept] == ["1"]
    assert duplicates == {"2": "1"}


@pytest.mark.asyncio
async def test_frame_extraction_preserves_original_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def write_frame(command: list[str], _: int) -> None:
        Image.new("RGB", (20, 10), color="blue").save(command[-1], format="JPEG")

    monkeypatch.setattr(
        "app.services.media.video_frames._run_ffmpeg",
        write_frame,
    )

    frames = await extract_selected_frames(
        tmp_path / "video.mp4",
        [2.25, 9.5],
        tmp_path / "frames",
        ffmpeg_path="ffmpeg",
        timeout_seconds=5,
        max_dimension=1280,
    )

    assert [frame.timestamp_seconds for frame in frames] == [2.25, 9.5]
    assert all(frame.path.is_file() for frame in frames)


@pytest.mark.asyncio
async def test_frame_extraction_rejects_invalid_timestamps(tmp_path: Path) -> None:
    with pytest.raises(MediaProcessingError) as error:
        await extract_selected_frames(
            tmp_path / "video.mp4",
            [float("nan")],
            tmp_path / "frames",
            ffmpeg_path="ffmpeg",
            timeout_seconds=5,
            max_dimension=1280,
        )

    assert error.value.code == "frame_timestamp_invalid"


@pytest.mark.asyncio
async def test_gemini_vision_sends_stateless_inline_jpeg_request(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-frame")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(await request.aread())
        assert payload["model"] == MODEL_ID
        assert payload["store"] is False
        assert payload["generation_config"]["max_output_tokens"] == 256
        assert payload["input"][1] == {
            "type": "image",
            "data": base64.b64encode(b"jpeg-frame").decode("ascii"),
            "mime_type": "image/jpeg",
        }
        return httpx.Response(
            200,
            headers={"x-goog-request-id": "vision-request"},
            json={
                "output_text": "A blue title slide.",
                "usage": {"input_tokens": 15, "output_tokens": 7},
            },
        )

    provider = GeminiVisionProvider(
        "test-only-key",
        timeout_seconds=5,
        max_image_bytes=1024,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.describe_frame(frame, "Hello")

    assert result.description == "A blue title slide."
    assert result.usage.input_tokens == 15
    assert result.usage.output_tokens == 7
    assert result.usage.request_id == "vision-request"
    assert requests[0].headers["Api-Revision"] == API_REVISION
    assert requests[0].headers["x-goog-api-key"] == "test-only-key"


@pytest.mark.asyncio
async def test_gemini_vision_classifies_quota_and_rejects_oversized_frame(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "30"})

    provider = GeminiVisionProvider(
        "test-only-key",
        timeout_seconds=5,
        max_image_bytes=4,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(VisionProviderError) as error:
        await provider.describe_frame(frame, "")

    assert error.value.code == "selected_frame_size_invalid"
    assert calls == 0

    provider = GeminiVisionProvider(
        "test-only-key",
        timeout_seconds=5,
        max_image_bytes=20,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VisionProviderError) as error:
        await provider.describe_frame(frame, "")

    assert error.value.quota is True
    assert error.value.retry_after is not None
    assert calls == 1


@pytest.mark.asyncio
async def test_frame_process_timeout_kills_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowProcess:
        returncode = -9
        killed = False

        async def wait(self) -> int:
            if not self.killed:
                await asyncio.sleep(0.1)
            return self.returncode

        def kill(self) -> None:
            self.killed = True

    process = SlowProcess()

    async def create_process(*_: object, **__: object) -> SlowProcess:
        return process

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_process,
    )

    with pytest.raises(MediaProcessingError) as error:
        await _run_ffmpeg(["ffmpeg"], 0)

    assert error.value.code == "frame_extraction_timeout"
    assert process.killed is True
