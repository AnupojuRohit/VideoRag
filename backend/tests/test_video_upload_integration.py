import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app import main
from app.api.routes import videos as video_routes
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.models.processing_job import ProcessingJob
from app.models.processing_stage import ProcessingStage
from app.models.provider_usage_event import ProviderUsageEvent
from app.models.embedding_index_version import EmbeddingIndexVersion
from app.models.enums import ChunkType, LifecycleStatus, StageStatus
from app.models.video_asset import VideoAsset
from app.models.video_chunk import VideoChunk
from app.services import media
from app.services.ingestion import pipeline
from app.services.ingestion import uploads
from app.services.jobs import dispatch
from app.services.jobs import orchestration
from app.services.media import audio
from app.services.indexing import shared as shared_indexing
from app.providers.transcription import (
    ProviderUsage,
    TranscribedWord,
    TranscriptionResult,
)
from app.providers.embedding import EmbeddingResult
from app.providers.errors import TranscriptionProviderError, VisionProviderError
from app.providers.vision import VisionResult
from app.services.media.scene_detection import DetectedScene
from app.services.media.video_frames import SelectedFrame
from app.services.media.audio import AudioSegment, MediaProcessingError
from app.services.retrieval import RetrievedEvidence, retrieve_video_evidence
from app.services.jobs.orchestration import CONSENT_VERSION

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
    class FakeEmbeddingProvider:
        async def embed_texts(
            self,
            texts: list[str],
            task_type: str,
        ) -> EmbeddingResult:
            assert texts
            assert task_type == "RETRIEVAL_DOCUMENT"
            return EmbeddingResult(
                vectors=[[1.0] + [0.0] * 767 for _ in texts],
                usage=ProviderUsage(
                    provider="test",
                    model_id="test-embedding",
                    operation="embedding",
                    status_code=200,
                    latency_ms=1,
                    input_bytes=sum(len(value.encode("utf-8")) for value in texts),
                    request_id="test-embedding-request",
                ),
            )

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
async def test_job_cannot_be_marked_ready_before_all_stages_complete(
    valid_mp4_bytes: bytes,
    temporary_media_root: Path,
    tracked_assets: list[UUID],
) -> None:
    async with await _client() as client:
        response = await client.post(
            "/api/v1/videos/upload?filename=not-ready.mp4",
            content=valid_mp4_bytes,
            headers=_upload_headers(valid_mp4_bytes),
        )
    assert response.status_code == 200, response.text
    upload = response.json()
    asset_id = UUID(upload["asset_id"])
    job_id = UUID(upload["job_id"])
    tracked_assets.append(asset_id)

    with pytest.raises(MediaProcessingError) as error:
        await pipeline._mark_job_ready(job_id)
    assert error.value.code == "required_processing_incomplete"

    async with SessionLocal() as session:
        job = await session.get(ProcessingJob, job_id)
        asset = await session.get(VideoAsset, asset_id)
    assert job is not None and job.status.value == "QUEUED"
    assert asset is not None and asset.status.value == "QUEUED"


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
    tracked_index_versions: list[UUID],
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
async def test_hybrid_retrieval_scopes_video_and_fuses_modalities(
    tracked_assets: list[UUID],
    tracked_index_versions: list[UUID],
) -> None:
    asset_id = uuid4()
    other_asset_id = uuid4()
    job_id = uuid4()
    index_id = uuid4()
    tracked_assets.extend([asset_id, other_asset_id])
    tracked_index_versions.append(index_id)

    def asset(asset_uuid: UUID) -> VideoAsset:
        return VideoAsset(
            id=asset_uuid,
            sha256_hash=hashlib.sha256(asset_uuid.bytes).hexdigest(),
            original_path="test-only",
            original_filename="retrieval-test.mp4",
            file_size_bytes=100,
            duration_seconds=20,
            mime_type="video/mp4",
            video_codec="h264",
            audio_codec="aac",
            width=320,
            height=240,
            status=LifecycleStatus.READY,
            external_processing_consent=True,
            external_processing_consent_version=CONSENT_VERSION,
        )

    async with SessionLocal() as session:
        session.add_all(
            [
                EmbeddingIndexVersion(
                    id=index_id,
                    model_id="gemini-embedding-001",
                    model_version="stable",
                    dimension=768,
                    is_active=True,
                ),
                asset(asset_id),
                asset(other_asset_id),
                ProcessingJob(
                    id=job_id,
                    video_id=asset_id,
                    status=LifecycleStatus.READY,
                    idempotency_key="retrieval-test",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                VideoChunk(
                    video_id=asset_id,
                    chunk_type=ChunkType.TRANSCRIPT,
                    content="Redis consumer groups coordinate multiple consumers.",
                    start_time=2,
                    end_time=4,
                    source_ref="transcript:00001",
                    embedding_index_version_id=index_id,
                    embedding=[1.0] + [0.0] * 767,
                ),
                VideoChunk(
                    video_id=asset_id,
                    chunk_type=ChunkType.VISUAL,
                    content="A slide diagram shows Redis consumer groups.",
                    start_time=12,
                    end_time=12,
                    source_ref="visual:frame:00001",
                    embedding_index_version_id=index_id,
                    embedding=[1.0] + [0.0] * 767,
                ),
                VideoChunk(
                    video_id=other_asset_id,
                    chunk_type=ChunkType.TRANSCRIPT,
                    content="Redis consumer groups from another video.",
                    start_time=1,
                    end_time=3,
                    source_ref="transcript:other",
                    embedding_index_version_id=index_id,
                    embedding=[1.0] + [0.0] * 767,
                ),
            ]
        )
        await session.commit()

    class FakeEmbeddingProvider:
        async def embed_texts(
            self,
            texts: list[str],
            task_type: str,
        ) -> EmbeddingResult:
            assert len(texts) == 1
            assert task_type == "RETRIEVAL_QUERY"
            return EmbeddingResult(
                vectors=[[1.0] + [0.0] * 767],
                usage=ProviderUsage(
                    provider="gemini",
                    model_id="gemini-embedding-001",
                    operation="embedding",
                    status_code=200,
                    latency_ms=1,
                    input_bytes=len(texts[0].encode("utf-8")),
                    request_id="retrieval-test-request",
                ),
            )

    result = await retrieve_video_evidence(
        job_id,
        asset_id,
        "consumer groups",
        limit=10,
        provider=FakeEmbeddingProvider(),
    )

    relevant_source_refs = {
        "transcript:00001",
        "visual:frame:00001",
    }
    retrieved_source_refs = {item.source_ref for item in result.evidence}
    recall_at_10 = len(relevant_source_refs & retrieved_source_refs) / len(
        relevant_source_refs
    )
    assert recall_at_10 == pytest.approx(1.0)
    assert retrieved_source_refs == relevant_source_refs
    assert {item.chunk_type for item in result.evidence} == {
        ChunkType.TRANSCRIPT,
        ChunkType.VISUAL,
    }
    transcript = next(
        item for item in result.evidence if item.chunk_type == ChunkType.TRANSCRIPT
    )
    visual = next(
        item for item in result.evidence if item.chunk_type == ChunkType.VISUAL
    )
    assert transcript.vector_rank is not None
    assert transcript.keyword_rank is not None
    assert visual.vector_rank is not None
    assert visual.keyword_rank is not None
    assert "speaker transcript" in result.context
    assert "visible frame/OCR/visual description" in result.context
    assert all("other" not in item.source_ref for item in result.evidence)


@pytest.mark.asyncio
async def test_library_and_answer_routes_require_ready_asset_and_current_consent(
    tracked_assets: list[UUID],
    monkeypatch,
) -> None:
    processing_id = uuid4()
    no_consent_id = uuid4()
    ready_id = uuid4()
    job_ids = [uuid4(), uuid4(), uuid4()]
    tracked_assets.extend([processing_id, no_consent_id, ready_id])

    def asset(
        asset_id: UUID,
        status: LifecycleStatus,
        *,
        consent: bool = False,
    ) -> VideoAsset:
        return VideoAsset(
            id=asset_id,
            sha256_hash=hashlib.sha256(asset_id.bytes).hexdigest(),
            original_path=f"/test/{asset_id}/source.mp4",
            original_filename="route-test.mp4",
            file_size_bytes=100,
            duration_seconds=20,
            mime_type="video/mp4",
            video_codec="h264",
            audio_codec="aac",
            width=320,
            height=240,
            status=status,
            external_processing_consent=consent,
            external_processing_consent_version=CONSENT_VERSION if consent else None,
        )

    async with SessionLocal() as session:
        session.add_all(
            [
                asset(processing_id, LifecycleStatus.PROCESSING),
                asset(no_consent_id, LifecycleStatus.READY),
                asset(ready_id, LifecycleStatus.READY, consent=True),
                ProcessingJob(
                    id=job_ids[0],
                    video_id=processing_id,
                    status=LifecycleStatus.PROCESSING,
                    idempotency_key="route-processing",
                ),
                ProcessingJob(
                    id=job_ids[1],
                    video_id=no_consent_id,
                    status=LifecycleStatus.READY,
                    idempotency_key="route-no-consent",
                ),
                ProcessingJob(
                    id=job_ids[2],
                    video_id=ready_id,
                    status=LifecycleStatus.READY,
                    idempotency_key="route-ready",
                ),
            ]
        )
        await session.commit()

    answer_calls: list[tuple[UUID, str]] = []

    async def fake_answer(
        job_id: UUID,
        video_id: UUID,
        question: str,
        *,
        limit: int,
    ) -> tuple[str, bool, list[RetrievedEvidence]]:
        answer_calls.append((job_id, question))
        return (
            "RRF combines ranked lists.",
            False,
            [
                RetrievedEvidence(
                    id=uuid4(),
                    chunk_type=ChunkType.TRANSCRIPT,
                    content="The speaker says RRF combines ranked lists.",
                    start_time=12.5,
                    end_time=16,
                    source_ref="transcript:00001",
                    vector_rank=1,
                    keyword_rank=1,
                    rrf_score=0.03,
                    citation_id="E1",
                )
            ],
        )

    monkeypatch.setattr(video_routes, "answer_video_question", fake_answer)
    async with await _client() as client:
        library = await client.get("/api/v1/videos")
        processing = await client.post(
            f"/api/v1/videos/{processing_id}/answer",
            json={"question": "What is RRF?"},
        )
        no_consent = await client.post(
            f"/api/v1/videos/{no_consent_id}/answer",
            json={"question": "What is RRF?"},
        )
        answer = await client.post(
            f"/api/v1/videos/{ready_id}/answer",
            json={"question": "What is RRF?"},
        )
        detail = await client.get(f"/api/v1/videos/{ready_id}")

    assert library.status_code == 200
    assert {item["id"] for item in library.json()["videos"]} >= {
        str(processing_id),
        str(no_consent_id),
        str(ready_id),
    }
    assert processing.status_code == 409
    assert processing.json()["error"]["code"] == "video_not_ready"
    assert no_consent.status_code == 409
    assert no_consent.json()["error"]["code"] == "external_processing_consent_required"
    assert answer.status_code == 200, answer.text
    assert answer.json()["citations"][0]["start_time"] == 12.5
    assert answer.json()["citations"][0]["citation_id"] == "E1"
    assert answer_calls == [(job_ids[2], "What is RRF?")]
    assert detail.status_code == 200
    assert detail.json()["external_processing_consent"] is True
    assert detail.json()["job"]["status"] == "READY"


@pytest.mark.asyncio
async def test_video_media_route_supports_byte_ranges_for_player_seeking(
    tmp_path: Path,
    tracked_assets: list[UUID],
    monkeypatch,
) -> None:
    asset_id = uuid4()
    tracked_assets.append(asset_id)
    media_path = tmp_path / "originals" / str(asset_id) / "source.mp4"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"0123456789")
    asset = VideoAsset(
        id=asset_id,
        sha256_hash=hashlib.sha256(asset_id.bytes).hexdigest(),
        original_path=str(media_path),
        original_filename="range-test.mp4",
        file_size_bytes=10,
        duration_seconds=20,
        mime_type="video/mp4",
        video_codec="h264",
        audio_codec="aac",
        width=320,
        height=240,
        status=LifecycleStatus.READY,
    )
    async with SessionLocal() as session:
        session.add(asset)
        await session.commit()

    monkeypatch.setattr(
        video_routes,
        "get_settings",
        lambda: Settings(media_root=tmp_path),
    )
    async with await _client() as client:
        response = await client.get(
            f"/api/v1/videos/{asset_id}/media",
            headers={"Range": "bytes=2-5"},
        )

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"


@pytest.mark.asyncio
async def test_audio_pipeline_persists_timestamped_local_chunks(
    valid_mp4_bytes: bytes,
    temporary_media_root: Path,
    tracked_assets: list[UUID],
    tracked_index_versions: list[UUID],
    monkeypatch,
) -> None:
    active_provider_calls = 0
    max_active_provider_calls = 0
    both_providers_started = asyncio.Event()
    embedding_calls = 0
    fail_final_segment = True
    indexed_before_final_segment = False
    transcription_calls: list[int] = []
    asset_id: UUID | None = None

    class FakeEmbeddingProvider:
        async def embed_texts(
            self,
            texts: list[str],
            task_type: str,
        ) -> EmbeddingResult:
            nonlocal embedding_calls
            embedding_calls += 1
            assert texts
            assert task_type == "RETRIEVAL_DOCUMENT"
            return EmbeddingResult(
                vectors=[[1.0] + [0.0] * 767 for _ in texts],
                usage=ProviderUsage(
                    provider="test",
                    model_id="test-embedding",
                    operation="embedding",
                    status_code=200,
                    latency_ms=1,
                    input_bytes=sum(len(value.encode("utf-8")) for value in texts),
                    request_id="test-embedding-request",
                ),
            )

    class FakeTranscriptionProvider:
        async def transcribe(self, audio_path: Path, mime_type: str) -> TranscriptionResult:
            nonlocal active_provider_calls
            nonlocal max_active_provider_calls
            nonlocal fail_final_segment
            nonlocal indexed_before_final_segment
            assert audio_path.is_file()
            assert mime_type == "audio/flac"
            segment_index = int(audio_path.stem.rsplit("-", maxsplit=1)[1])
            transcription_calls.append(segment_index)
            active_provider_calls += 1
            max_active_provider_calls = max(
                max_active_provider_calls,
                active_provider_calls,
            )
            if segment_index == 0 and active_provider_calls == 2:
                both_providers_started.set()
            if segment_index == 0:
                await asyncio.wait_for(both_providers_started.wait(), timeout=2)
            active_provider_calls -= 1
            if segment_index == 2:
                assert asset_id is not None
                async with SessionLocal() as session:
                    first_chunk = await session.scalar(
                        select(VideoChunk).where(
                            VideoChunk.video_id == asset_id,
                            VideoChunk.chunk_type == ChunkType.TRANSCRIPT,
                            VideoChunk.source_ref == "transcript:00000",
                        )
                    )
                    transcription_status = await session.scalar(
                        select(ProcessingStage.status).where(
                            ProcessingStage.job_id == job_id,
                            ProcessingStage.name == "transcription",
                        )
                    )
                indexed_before_final_segment = (
                    first_chunk is not None
                    and first_chunk.content == "Hello."
                    and transcription_status == StageStatus.RUNNING
                )
                if fail_final_segment:
                    fail_final_segment = False
                    raise TranscriptionProviderError(
                        "test_later_segment_failure",
                        "The final test segment failed.",
                        retryable=True,
                    )
            text, words = {
                0: (
                    "Hello.",
                    [TranscribedWord("Hello.", 0.1, 0.4)],
                ),
                1: (
                    "world.",
                    [TranscribedWord("world.", 1.5, 1.8)],
                ),
                2: (
                    "Again.",
                    [TranscribedWord("Again.", 2.5, 2.8)],
                ),
            }[segment_index]
            return TranscriptionResult(
                text=text,
                words=words,
                usage=ProviderUsage(
                    provider="test",
                    model_id="test-transcriber",
                    operation="transcription",
                    status_code=200,
                    latency_ms=1,
                    input_bytes=10,
                    request_id=f"test-request-{segment_index}",
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

    async def fake_create_audio_segments(
        context,
        _: Path,
        __: float,
    ) -> list[AudioSegment]:
        segments = []
        for index, start_seconds in enumerate((0.0, 4.0, 8.0)):
            path = context.work_dir / "audio" / f"segment-{index:05d}.flac"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test audio")
            segments.append(
                AudioSegment(
                    index=index,
                    path=path,
                    start_seconds=start_seconds,
                    duration_seconds=6.0,
                )
            )
        return segments

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
    monkeypatch.setattr(
        pipeline,
        "_create_audio_segments",
        fake_create_audio_segments,
    )
    monkeypatch.setattr(pipeline, "extract_selected_frames", fake_extract_frames)
    monkeypatch.setattr(pipeline, "extract_visible_text", lambda _: "test slide text")
    monkeypatch.setattr(
        shared_indexing,
        "create_embedding_provider",
        FakeEmbeddingProvider,
    )
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
        previous_active_version = await session.scalar(
            select(EmbeddingIndexVersion.id).where(
                EmbeddingIndexVersion.is_active.is_(True)
            )
        )

    async with SessionLocal() as session:
        asset = await session.get(VideoAsset, asset_id)
        assert asset is not None
        asset.duration_seconds = 20
        asset.audio_start_time = 2
        await session.commit()

    await pipeline.process_ingestion_job(job_id)

    assert indexed_before_final_segment
    async with SessionLocal() as session:
        failed_job = await session.get(ProcessingJob, job_id)
        failed_asset = await session.get(VideoAsset, asset_id)
        first_chunk = await session.scalar(
            select(VideoChunk).where(
                VideoChunk.video_id == asset_id,
                VideoChunk.chunk_type == ChunkType.TRANSCRIPT,
                VideoChunk.source_ref == "transcript:00000",
            )
        )
    assert failed_job is not None and failed_job.status == LifecycleStatus.FAILED
    assert failed_asset is not None and failed_asset.status == LifecycleStatus.FAILED
    assert first_chunk is not None and first_chunk.content == "Hello."

    retry = await orchestration.retry_job(job_id)
    assert retry.status == LifecycleStatus.QUEUED
    await pipeline.process_ingestion_job(job_id)
    assert transcription_calls == [0, 1, 2, 2]

    async with SessionLocal() as session:
        active_version = await session.scalar(
            select(EmbeddingIndexVersion).where(
                EmbeddingIndexVersion.is_active.is_(True)
            )
        )
        if previous_active_version is None and active_version is not None:
            tracked_index_versions.append(active_version.id)
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

    assert job.status.value == "READY"
    assert job.current_stage is None
    assert job.lease_expires_at is None
    assert asset is not None and asset.status.value == "READY"
    assert stages["audio_extraction"].value == "COMPLETED"
    assert stages["audio_segmentation"].value == "COMPLETED"
    assert stages["transcription"].value == "COMPLETED"
    assert stages["transcript_chunking"].value == "COMPLETED"
    assert stages["scene_detection"].value == "COMPLETED"
    assert stages["frame_extraction"].value == "COMPLETED"
    assert stages["ocr"].value == "COMPLETED"
    assert stages["visual_analysis"].value == "COMPLETED"
    assert stages["embeddings"].value == "COMPLETED"
    assert stages["indexing"].value == "COMPLETED"
    assert stages["verification"].value == "COMPLETED"
    assert usage_count >= 6
    assert max_active_provider_calls == 2
    assert embedding_calls >= 3
    chunks_path = (
        temporary_media_root
        / "work"
        / str(asset_id)
        / "audio"
        / "transcript-chunks.json"
    )
    transcript_chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    assert transcript_chunks["timeline"] == "original_video"
    assert [chunk["content"] for chunk in transcript_chunks["chunks"]] == [
        "Hello.",
        "world.",
        "Again.",
    ]
    assert transcript_chunks["chunks"][0]["start_time"] == 2.1
    assert transcript_chunks["chunks"][0]["end_time"] == 2.4
    assert transcript_chunks["chunks"][1]["start_time"] == 7.5
    assert transcript_chunks["chunks"][2]["start_time"] == 12.5
    embedding_calls_before_retry = embedding_calls
    assert (
        await shared_indexing.index_evidence(
            job_id,
            asset_id,
            ChunkType.TRANSCRIPT,
            transcript_chunks["chunks"],
            provider=FakeEmbeddingProvider(),
        )
        == 0
    )
    assert embedding_calls == embedding_calls_before_retry
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
    async with SessionLocal() as session:
        chunks = (
            await session.scalars(
                select(VideoChunk).where(VideoChunk.video_id == asset_id)
            )
        ).all()
        assert {chunk.chunk_type for chunk in chunks} == {
            ChunkType.TRANSCRIPT,
            ChunkType.VISUAL,
        }
        assert all(chunk.embedding is not None for chunk in chunks)
        assert all(len(chunk.embedding) == 768 for chunk in chunks)
        assert all(str(chunk.search_vector) for chunk in chunks)
        transcript_refs = [
            chunk.source_ref
            for chunk in chunks
            if chunk.chunk_type == ChunkType.TRANSCRIPT
        ]
        assert len(transcript_refs) == 3
        assert len(set(transcript_refs)) == 3


@pytest.mark.asyncio
async def test_visual_failure_preserves_completed_audio_artifacts(
    valid_mp4_bytes: bytes,
    temporary_media_root: Path,
    tracked_assets: list[UUID],
    tracked_index_versions: list[UUID],
    monkeypatch,
) -> None:
    class FakeEmbeddingProvider:
        async def embed_texts(
            self,
            texts: list[str],
            task_type: str,
        ) -> EmbeddingResult:
            assert texts
            assert task_type == "RETRIEVAL_DOCUMENT"
            return EmbeddingResult(
                vectors=[[1.0] + [0.0] * 767 for _ in texts],
                usage=ProviderUsage(
                    provider="test",
                    model_id="test-embedding",
                    operation="embedding",
                    status_code=200,
                    latency_ms=1,
                    input_bytes=sum(len(value.encode("utf-8")) for value in texts),
                    request_id="test-embedding-request",
                ),
            )

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
    monkeypatch.setattr(
        shared_indexing,
        "create_embedding_provider",
        FakeEmbeddingProvider,
    )

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
        previous_active_version = await session.scalar(
            select(EmbeddingIndexVersion.id).where(
                EmbeddingIndexVersion.is_active.is_(True)
            )
        )

    async with SessionLocal() as session:
        asset = await session.get(VideoAsset, asset_id)
        assert asset is not None
        asset.duration_seconds = 10
        await session.commit()

    await pipeline.process_ingestion_job(job_id)

    async with SessionLocal() as session:
        active_version = await session.scalar(
            select(EmbeddingIndexVersion).where(
                EmbeddingIndexVersion.is_active.is_(True)
            )
        )
        if previous_active_version is None and active_version is not None:
            tracked_index_versions.append(active_version.id)
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
        chunk_types = set(
            (
                await session.scalars(
                    select(VideoChunk.chunk_type).where(
                        VideoChunk.video_id == asset_id
                    )
                )
            ).all()
        )

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
    assert stages["embeddings"].status.value == "FAILED"
    assert ChunkType.TRANSCRIPT in chunk_types
    assert ChunkType.VISUAL not in chunk_types
    assert transcript_path.is_file()
    assert json.loads(transcript_path.read_text(encoding="utf-8"))["chunks"]


@pytest.mark.asyncio
async def test_job_reconciler_redispatches_expired_lease_once(
    tracked_assets: list[UUID],
    monkeypatch,
) -> None:
    asset_id = uuid4()
    job_id = uuid4()
    tracked_assets.append(asset_id)
    now = datetime.now(timezone.utc)
    asset = VideoAsset(
        id=asset_id,
        sha256_hash=hashlib.sha256(asset_id.bytes).hexdigest(),
        original_path="test-only",
        original_filename="recovery-test.mp4",
        file_size_bytes=100,
        duration_seconds=10,
        mime_type="video/mp4",
        video_codec="h264",
        audio_codec="aac",
        width=320,
        height=240,
        status=LifecycleStatus.PROCESSING,
        external_processing_consent=True,
        external_processing_consent_version=CONSENT_VERSION,
    )
    job = ProcessingJob(
        id=job_id,
        video_id=asset_id,
        status=LifecycleStatus.PROCESSING,
        current_stage="transcription",
        idempotency_key="recovery-test",
        attempt_count=1,
        lease_expires_at=now - timedelta(seconds=1),
    )
    async with SessionLocal() as session:
        session.add_all([asset, job])
        await session.commit()

    published: list[tuple[str, list[str]]] = []

    def record_task(task_name: str, *, args: list[str]) -> None:
        published.append((task_name, args))

    monkeypatch.setattr(dispatch.celery_app, "send_task", record_task)

    assert await dispatch.reconcile_jobs() == 1
    assert published == [(dispatch.TASK_NAME, [str(job_id)])]
    assert await dispatch.reconcile_jobs() == 0

    async with SessionLocal() as session:
        persisted_job = await session.get(ProcessingJob, job_id)
        persisted_asset = await session.get(VideoAsset, asset_id)
        assert persisted_job is not None
        assert persisted_asset is not None
        assert persisted_job.status == LifecycleStatus.PROCESSING
        assert persisted_job.last_enqueued_at is not None
        assert persisted_asset.status == LifecycleStatus.QUEUED


@pytest.mark.asyncio
async def test_retry_resets_failed_stage_and_cancel_stops_queued_job(
    tracked_assets: list[UUID],
    monkeypatch,
) -> None:
    asset_id = uuid4()
    job_id = uuid4()
    tracked_assets.append(asset_id)
    now = datetime.now(timezone.utc)
    asset = VideoAsset(
        id=asset_id,
        sha256_hash=hashlib.sha256(asset_id.bytes).hexdigest(),
        original_path="test-only",
        original_filename="retry-test.mp4",
        file_size_bytes=100,
        duration_seconds=10,
        mime_type="video/mp4",
        video_codec="h264",
        audio_codec="aac",
        width=320,
        height=240,
        status=LifecycleStatus.FAILED,
        external_processing_consent=True,
        external_processing_consent_version=CONSENT_VERSION,
    )
    job = ProcessingJob(
        id=job_id,
        video_id=asset_id,
        status=LifecycleStatus.FAILED,
        current_stage="transcription",
        idempotency_key="retry-test",
        attempt_count=1,
        last_error={"code": "temporary_provider_error", "retryable": True},
        finished_at=now,
    )
    stage = ProcessingStage(
        job_id=job_id,
        name="transcription",
        status=StageStatus.FAILED,
        attempt_count=1,
        last_error={"code": "temporary_provider_error", "retryable": True},
        finished_at=now,
    )
    async with SessionLocal() as session:
        session.add_all([asset, job, stage])
        await session.commit()

    async def accept_schedule(scheduled_job_id: UUID) -> LifecycleStatus:
        assert scheduled_job_id == job_id
        return LifecycleStatus.QUEUED

    monkeypatch.setattr(orchestration, "schedule_job", accept_schedule)

    retried = await orchestration.retry_job(job_id)
    assert retried.status == LifecycleStatus.QUEUED
    retry_stage = next(
        stage for stage in retried.stages if stage.name == "transcription"
    )
    assert retry_stage.status == StageStatus.PENDING

    cancelled = await orchestration.cancel_job(job_id)
    assert cancelled.status == LifecycleStatus.CANCELLED
    assert not cancelled.cancel_available
    cancelled_stage = next(
        stage for stage in cancelled.stages if stage.name == "transcription"
    )
    assert cancelled_stage.status == StageStatus.CANCELLED
