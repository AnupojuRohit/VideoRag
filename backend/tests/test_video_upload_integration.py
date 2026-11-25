import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app import main
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.models.processing_job import ProcessingJob
from app.models.processing_stage import ProcessingStage
from app.models.provider_usage_event import ProviderUsageEvent
from app.models.embedding_index_version import EmbeddingIndexVersion
from app.models.enums import ChunkType
from app.models.video_asset import VideoAsset
from app.models.video_chunk import VideoChunk
from app.services import media
from app.services.ingestion import pipeline
from app.services.ingestion import uploads
from app.services.jobs import dispatch
from app.services.media import audio
from app.providers.transcription import (
    ProviderUsage,
    TranscribedWord,
    TranscriptionResult,
)
from app.providers.errors import VisionProviderError
from app.providers.vision import VisionResult
from app.services.media.scene_detection import DetectedScene
from app.services.media.video_frames import SelectedFrame

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="Set RUN_DB_INTEGRATION_TESTS=1 to use the local PostgreSQL database.",
    ),
]


@pytest_asyncio.fixture
async def tracked_index_versions() -> list[UUID]:
    return []


@pytest_asyncio.fixture
async def tracked_assets(tracked_index_versions: list[UUID]) -> list[UUID]:
    await engine.dispose()
    asset_ids: list[UUID] = []
    yield asset_ids
    if asset_ids or tracked_index_versions:
        async with SessionLocal() as session:
            if asset_ids:
                await session.execute(
                    delete(VideoAsset).where(VideoAsset.id.in_(asset_ids))
                )
            if tracked_index_versions:
                await session.execute(
                    delete(EmbeddingIndexVersion).where(
                        EmbeddingIndexVersion.id.in_(tracked_index_versions)
                    )
                )
            await session.commit()
    await engine.dispose()


@pytest.fixture
def temporary_media_root(tmp_path: Path, monkeypatch) -> Path:
    settings = Settings(media_root=tmp_path)
    monkeypatch.setattr(uploads, "get_settings", lambda: settings)
    monkeypatch.setattr(media.ffprobe, "get_settings", lambda: settings)
    monkeypatch.setattr(audio, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatch.celery_app, "send_task", lambda *args, **kwargs: None)
    return tmp_path


@pytest.fixture
def valid_mp4_bytes(tmp_path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required for upload integration fixtures.")
    video_path = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=44100",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-y",
            str(video_path),
        ],
        check=True,
        capture_output=True,
    )
    return video_path.read_bytes()


def _upload_headers(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "video/mp4",
        "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
        "X-External-Processing-Consent": "true",
    }


async def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=main.app),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_upload_and_different_filename_reuse_same_asset(
    valid_mp4_bytes: bytes,
    temporary_media_root: Path,
    tracked_assets: list[UUID],
) -> None:
    digest = hashlib.sha256(valid_mp4_bytes).hexdigest()
    async with await _client() as client:
        preflight = await client.post(
            "/api/v1/videos/preflight",
            json={"sha256_hash": digest},
        )
        assert preflight.status_code == 200
        assert preflight.json()["can_upload"] is True

        first = await client.post(
            "/api/v1/videos/upload?filename=lecture-one.mp4",
            content=valid_mp4_bytes,
            headers=_upload_headers(valid_mp4_bytes),
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        tracked_assets.append(UUID(first_body["asset_id"]))
        assert first_body["status"] == "QUEUED"
        assert first_body["duplicate"] is False

        second = await client.post(
            "/api/v1/videos/upload?filename=renamed-copy.mp4",
            content=valid_mp4_bytes,
            headers=_upload_headers(valid_mp4_bytes),
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["asset_id"] == first_body["asset_id"]
        assert second_body["job_id"] == first_body["job_id"]
        assert second_body["duplicate"] is True
        assert second_body["duplicate_action"] == "attach_existing_job"

        existing = await client.post(
            "/api/v1/videos/preflight",
            json={"sha256_hash": digest},
        )
        assert existing.json()["asset_id"] == first_body["asset_id"]
        assert existing.json()["duplicate_action"] == "attach_existing_job"

    async with SessionLocal() as session:
        job_count = await session.scalar(
            select(func.count(ProcessingJob.id)).where(
                ProcessingJob.video_id == tracked_assets[0]
            )
        )
        stage_count = await session.scalar(
            select(func.count(ProcessingStage.id)).where(
                ProcessingStage.job_id == UUID(first_body["job_id"])
            )
        )
        asset = await session.get(VideoAsset, tracked_assets[0])

    assert job_count == 1
    assert stage_count == 13
    assert asset is not None
    assert Path(asset.original_path).is_file()
    assert Path(asset.original_path).read_bytes() == valid_mp4_bytes
    assert (temporary_media_root / ".staging").is_dir()
    assert list((temporary_media_root / ".staging").iterdir()) == []


@pytest.mark.asyncio
async def test_concurrent_same_bytes_uploads_create_one_asset(
    valid_mp4_bytes: bytes,
    tracked_assets: list[UUID],
) -> None:
    async with await _client() as client:
        results = await asyncio.gather(
            *(
                client.post(
                    f"/api/v1/videos/upload?filename=parallel-{index}.mp4",
                    content=valid_mp4_bytes,
                    headers=_upload_headers(valid_mp4_bytes),
                )
                for index in range(2)
            )
        )

    assert [response.status_code for response in results] == [200, 200], [
        response.text for response in results
    ]
    payloads = [response.json() for response in results]
    assert payloads[0]["asset_id"] == payloads[1]["asset_id"]
    assert payloads[0]["job_id"] == payloads[1]["job_id"]
    tracked_assets.append(UUID(payloads[0]["asset_id"]))
    assert sorted(payload["duplicate"] for payload in payloads) == [False, True]

    async with SessionLocal() as session:
        asset_count = await session.scalar(
            select(func.count(VideoAsset.id)).where(
                VideoAsset.sha256_hash == hashlib.sha256(valid_mp4_bytes).hexdigest()
            )
        )
    assert asset_count == 1


@pytest.mark.asyncio
async def test_hash_mismatch_is_rejected_without_persisting_asset(
    valid_mp4_bytes: bytes,
) -> None:
    wrong_hash = "0" * 64
    async with await _client() as client:
        response = await client.post(
            "/api/v1/videos/upload?filename=hash-mismatch.mp4",
            content=valid_mp4_bytes,
            headers={
                "Content-Type": "video/mp4",
                "X-Content-SHA256": wrong_hash,
                "X-External-Processing-Consent": "true",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "hash_mismatch"


@pytest.mark.asyncio
async def test_invalid_mp4_is_rejected(
    temporary_media_root: Path,
) -> None:
    body = b"not an mp4"
    async with await _client() as client:
        response = await client.post(
            "/api/v1/videos/upload?filename=corrupt.mp4",
            content=body,
            headers=_upload_headers(body),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_media"
    assert list((temporary_media_root / ".staging").iterdir()) == []


@pytest.mark.asyncio
async def test_upload_enforces_configured_byte_limit(
    temporary_media_root: Path,
    monkeypatch,
) -> None:
    body = b"too many bytes"
    settings = Settings(media_root=temporary_media_root, max_upload_bytes=8)
    monkeypatch.setattr(uploads, "get_settings", lambda: settings)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/videos/upload?filename=oversized.mp4",
            content=body,
            headers=_upload_headers(body),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert list((temporary_media_root / ".staging").iterdir()) == []


@pytest.mark.asyncio
async def test_upload_enforces_configured_duration_limit(
    valid_mp4_bytes: bytes,
    temporary_media_root: Path,
    tracked_assets: list[UUID],
    monkeypatch,
) -> None:
    settings = Settings(
        media_root=temporary_media_root,
        max_video_duration_seconds=0.5,
    )
    monkeypatch.setattr(uploads, "get_settings", lambda: settings)
    monkeypatch.setattr(media.ffprobe, "get_settings", lambda: settings)
    async with await _client() as client:
        response = await client.post(
            "/api/v1/videos/upload?filename=long.mp4",
            content=valid_mp4_bytes,
            headers=_upload_headers(valid_mp4_bytes),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "video_too_long"
    assert list((temporary_media_root / ".staging").iterdir()) == []


@pytest.mark.asyncio
async def test_database_maintains_fts_and_rejects_wrong_embedding_dimension(
    tracked_assets: list[UUID],
    tracked_index_versions: list[UUID],
) -> None:
    asset_id = uuid4()
    tracked_assets.append(asset_id)
    index_id = uuid4()
    tracked_index_versions.append(index_id)

    async with SessionLocal() as session:
        asset = VideoAsset(
            id=asset_id,
            sha256_hash=hashlib.sha256(asset_id.bytes).hexdigest(),
            original_path="test-only",
            original_filename="test.mp4",
            file_size_bytes=100,
            duration_seconds=1,
            mime_type="video/mp4",
            video_codec="h264",
            audio_codec="aac",
            width=320,
            height=240,
            status="UPLOADED",
        )
        index = EmbeddingIndexVersion(
            id=index_id,
            model_id=f"phase12-test-{asset_id}",
            model_version="test-only-v1",
            dimension=3,
            is_active=False,
        )
        session.add_all([asset, index])
        await session.flush()
        chunk = VideoChunk(
            video_id=asset_id,
            chunk_type=ChunkType.TRANSCRIPT,
            content="Redis consumer groups coordinate consumers.",
            start_time=0,
            end_time=1,
            source_ref="test:transcript:0",
            embedding_index_version_id=index_id,
            embedding=[0.1, 0.2, 0.3],
        )
        session.add(chunk)
        await session.flush()
        chunk_id = chunk.id
        await session.commit()

    async with SessionLocal() as session:
        result = await session.execute(
            select(VideoChunk.search_vector, VideoChunk.embedding).where(
                VideoChunk.id == chunk_id
            )
        )
        search_vector, embedding = result.one()
        assert "redis" in str(search_vector)
        assert len(embedding) == 3

    async with SessionLocal() as session:
        session.add(
            VideoChunk(
                video_id=asset_id,
                chunk_type=ChunkType.VISUAL,
                content="This vector has the wrong dimension.",
                start_time=0,
                end_time=0,
                source_ref="test:visual:wrong-dimension",
                embedding_index_version_id=index_id,
                embedding=[0.1, 0.2],
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_audio_pipeline_persists_timestamped_local_chunks(
    valid_mp4_bytes: bytes,
    temporary_media_root: Path,
    tracked_assets: list[UUID],
    monkeypatch,
) -> None:
    active_provider_calls = 0
    max_active_provider_calls = 0
    both_providers_started = asyncio.Event()

    class FakeTranscriptionProvider:
        async def transcribe(self, audio_path: Path, mime_type: str) -> TranscriptionResult:
            nonlocal active_provider_calls, max_active_provider_calls
            assert audio_path.is_file()
            assert mime_type == "audio/flac"
            active_provider_calls += 1
            max_active_provider_calls = max(
                max_active_provider_calls,
                active_provider_calls,
            )
            if active_provider_calls == 2:
                both_providers_started.set()
            await asyncio.wait_for(both_providers_started.wait(), timeout=2)
            active_provider_calls -= 1
            return TranscriptionResult(
                text="Hello world",
                words=[
                    TranscribedWord("Hello", 0.1, 0.4),
                    TranscribedWord("world.", 0.5, 0.8),
                ],
                usage=ProviderUsage(
                    provider="test",
                    model_id="test-transcriber",
                    operation="transcription",
                    status_code=200,
                    latency_ms=1,
                    input_bytes=10,
                    request_id="test-request",
                ),
            )

    class FakeVisionProvider:
        async def describe_frame(self, image_path: Path, ocr_text: str) -> VisionResult:
            nonlocal active_provider_calls, max_active_provider_calls
            assert image_path.is_file()
            assert ocr_text == "test slide text"
            active_provider_calls += 1
            max_active_provider_calls = max(
                max_active_provider_calls,
                active_provider_calls,
            )
            if active_provider_calls == 2:
                both_providers_started.set()
            await asyncio.wait_for(both_providers_started.wait(), timeout=2)
            active_provider_calls -= 1
            return VisionResult(
                description="A test slide with a blue heading.",
                usage=ProviderUsage(
                    provider="test",
                    model_id="test-vision",
                    operation="vision",
                    status_code=200,
                    latency_ms=1,
                    input_bytes=10,
                    request_id="test-vision-request",
                ),
            )

    async def fake_detect_scenes(
        _: Path,
        __: float,
        ___: int,
    ) -> list[DetectedScene]:
        return [DetectedScene(start_seconds=0, end_seconds=1)]

    async def fake_extract_frames(
        _: Path,
        timestamps: list[float],
        output_dir: Path,
        **__: object,
    ) -> list[SelectedFrame]:
        output_dir.mkdir(parents=True, exist_ok=True)
        frame_path = output_dir / "frame-00000.jpg"
        from PIL import Image

        Image.new("RGB", (16, 16), color="blue").save(frame_path)
        return [
            SelectedFrame(
                index=0,
                timestamp_seconds=timestamps[0],
                path=frame_path,
            )
        ]

    monkeypatch.setattr(
        pipeline,
        "create_transcription_provider",
        FakeTranscriptionProvider,
    )
    monkeypatch.setattr(
        pipeline,
        "create_vision_provider",
        FakeVisionProvider,
    )
    monkeypatch.setattr(pipeline, "detect_scenes", fake_detect_scenes)
    monkeypatch.setattr(pipeline, "extract_selected_frames", fake_extract_frames)
    monkeypatch.setattr(pipeline, "extract_visible_text", lambda _: "test slide text")
    async with await _client() as client:
        response = await client.post(
            "/api/v1/videos/upload?filename=audio-pipeline.mp4",
            content=valid_mp4_bytes,
            headers=_upload_headers(valid_mp4_bytes),
        )
    assert response.status_code == 200, response.text
    upload = response.json()
    asset_id = UUID(upload["asset_id"])
    job_id = UUID(upload["job_id"])
    tracked_assets.append(asset_id)

    async with SessionLocal() as session:
        asset = await session.get(VideoAsset, asset_id)
        assert asset is not None
        asset.duration_seconds = 10
        asset.audio_start_time = 2
        await session.commit()

    await pipeline.process_ingestion_job(job_id)

    async with SessionLocal() as session:
        job = await session.get(ProcessingJob, job_id)
        assert job is not None
        stages = {
            stage.name: stage.status
            for stage in (
                await session.scalars(
                    select(ProcessingStage).where(
                        ProcessingStage.job_id == job_id
                    )
                )
            ).all()
        }
        asset = await session.get(VideoAsset, asset_id)
        usage_count = await session.scalar(
            select(func.count(ProviderUsageEvent.id)).where(
                ProviderUsageEvent.job_id == job_id
            )
        )

    assert job.status.value == "PROCESSING"
    assert job.current_stage is None
    assert job.lease_expires_at is None
    assert asset is not None and asset.status.value == "PROCESSING"
    assert stages["audio_extraction"].value == "COMPLETED"
    assert stages["audio_segmentation"].value == "COMPLETED"
    assert stages["transcription"].value == "COMPLETED"
    assert stages["transcript_chunking"].value == "COMPLETED"
    assert stages["scene_detection"].value == "COMPLETED"
    assert stages["frame_extraction"].value == "COMPLETED"
    assert stages["ocr"].value == "COMPLETED"
    assert stages["visual_analysis"].value == "COMPLETED"
    assert usage_count == 2
    assert max_active_provider_calls == 2
    chunks_path = (
        temporary_media_root
        / "work"
        / str(asset_id)
        / "audio"
        / "transcript-chunks.json"
    )
    transcript_chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    assert transcript_chunks["timeline"] == "original_video"
    assert transcript_chunks["chunks"][0]["start_time"] == 2.1
    assert transcript_chunks["chunks"][0]["end_time"] == 2.8
    visual_path = (
        temporary_media_root
        / "work"
        / str(asset_id)
        / "visual"
        / "visual-evidence.json"
    )
    visual_evidence = json.loads(visual_path.read_text(encoding="utf-8"))
    assert visual_evidence["timeline"] == "original_video"
    assert visual_evidence["evidence"][0]["start_time"] == 0.5


@pytest.mark.asyncio
async def test_visual_failure_preserves_completed_audio_artifacts(
    valid_mp4_bytes: bytes,
    temporary_media_root: Path,
    tracked_assets: list[UUID],
    monkeypatch,
) -> None:
    class FakeTranscriptionProvider:
        async def transcribe(
            self,
            audio_path: Path,
            mime_type: str,
        ) -> TranscriptionResult:
            assert audio_path.is_file()
            return TranscriptionResult(
                text="Audio evidence",
                words=[TranscribedWord("Audio", 0.1, 0.3)],
                usage=ProviderUsage(
                    provider="test",
                    model_id="test-transcriber",
                    operation="transcription",
                    status_code=200,
                    latency_ms=1,
                    input_bytes=10,
                    request_id="test-audio-request",
                ),
            )

    class FailingVisionProvider:
        async def describe_frame(self, image_path: Path, ocr_text: str) -> VisionResult:
            raise VisionProviderError(
                "vision_rejected",
                "The test vision request was rejected.",
                retryable=False,
            )

    async def fake_detect_scenes(
        _: Path,
        __: float,
        ___: int,
    ) -> list[DetectedScene]:
        return [DetectedScene(start_seconds=0, end_seconds=1)]

    async def fake_extract_frames(
        _: Path,
        timestamps: list[float],
        output_dir: Path,
        **__: object,
    ) -> list[SelectedFrame]:
        output_dir.mkdir(parents=True, exist_ok=True)
        frame_path = output_dir / "frame-00000.jpg"
        from PIL import Image

        Image.new("RGB", (16, 16), color="blue").save(frame_path)
        return [
            SelectedFrame(
                index=0,
                timestamp_seconds=timestamps[0],
                path=frame_path,
            )
        ]

    monkeypatch.setattr(
        pipeline,
        "create_transcription_provider",
        FakeTranscriptionProvider,
    )
    monkeypatch.setattr(
        pipeline,
        "create_vision_provider",
        FailingVisionProvider,
    )
    monkeypatch.setattr(pipeline, "detect_scenes", fake_detect_scenes)
    monkeypatch.setattr(pipeline, "extract_selected_frames", fake_extract_frames)
    monkeypatch.setattr(pipeline, "extract_visible_text", lambda _: "visible words")

    async with await _client() as client:
        response = await client.post(
            "/api/v1/videos/upload?filename=visual-failure.mp4",
            content=valid_mp4_bytes,
            headers=_upload_headers(valid_mp4_bytes),
        )
    assert response.status_code == 200, response.text
    upload = response.json()
    asset_id = UUID(upload["asset_id"])
    job_id = UUID(upload["job_id"])
    tracked_assets.append(asset_id)

    async with SessionLocal() as session:
        asset = await session.get(VideoAsset, asset_id)
        assert asset is not None
        asset.duration_seconds = 10
        await session.commit()

    await pipeline.process_ingestion_job(job_id)

    async with SessionLocal() as session:
        job = await session.get(ProcessingJob, job_id)
        assert job is not None
        stages = {
            stage.name: stage
            for stage in (
                await session.scalars(
                    select(ProcessingStage).where(
                        ProcessingStage.job_id == job_id
                    )
                )
            ).all()
        }

    transcript_path = (
        temporary_media_root
        / "work"
        / str(asset_id)
        / "audio"
        / "transcript-chunks.json"
    )
    assert job.status.value == "FAILED"
    assert stages["transcript_chunking"].status.value == "COMPLETED"
    assert stages["visual_analysis"].status.value == "FAILED"
    assert transcript_path.is_file()
    assert json.loads(transcript_path.read_text(encoding="utf-8"))["chunks"]
