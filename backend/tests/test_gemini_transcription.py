import json
from pathlib import Path

import httpx
import pytest

from app.providers.errors import TranscriptionProviderError
from app.providers.gemini_transcription import (
    MODEL_ID,
    GeminiTranscriptionProvider,
)


@pytest.mark.asyncio
async def test_gemini_transcription_uses_word_timestamps_and_deletes_file(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "segment.flac"
    audio_path.write_bytes(b"test audio")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/upload/v1beta/files":
            return httpx.Response(
                200,
                headers={
                    "X-Goog-Upload-URL": (
                        "https://generativelanguage.googleapis.com/upload/test-session"
                    )
                },
            )
        if request.url.path == "/upload/test-session":
            assert await request.aread() == b"test audio"
            return httpx.Response(
                200,
                json={
                    "file": {
                        "name": "files/test-id",
                        "uri": "https://generativelanguage.googleapis.com/files/test-id",
                    }
                },
            )
        if request.url.path == "/v1beta/interactions":
            payload = json.loads(await request.aread())
            assert payload["model"] == f"models/{MODEL_ID}"
            assert payload["store"] is False
            assert payload["generation_config"]["transcription_config"]["mode"][
                "timestamp_granularities"
            ] == ["word"]
            return httpx.Response(
                200,
                json={
                    "id": "interactions/test-id",
                    "status": "completed",
                    "output_text": "Hello world",
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Hello world",
                                    "annotations": [
                                        {
                                            "type": "word_info",
                                            "text": "Hello",
                                            "start_offset": "0.100s",
                                            "end_offset": "0.400s",
                                        },
                                        {
                                            "type": "word_info",
                                            "text": "world",
                                            "start_offset": "0.500s",
                                            "end_offset": "0.800s",
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    provider = GeminiTranscriptionProvider(
        "test-only-key",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.transcribe(audio_path, "audio/flac")

    assert result.text == "Hello world"
    assert [(word.text, word.start_seconds, word.end_seconds) for word in result.words] == [
        ("Hello", 0.1, 0.4),
        ("world", 0.5, 0.8),
    ]
    assert result.usage.model_id == MODEL_ID
    assert result.usage.input_bytes == len(b"test audio")
    assert [request.method for request in requests] == ["POST", "POST", "POST", "DELETE"]
    assert all(request.headers["x-goog-api-key"] == "test-only-key" for request in requests)


@pytest.mark.asyncio
async def test_gemini_quota_response_is_classified_for_safe_pause(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "segment.flac"
    audio_path.write_bytes(b"test audio")

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    provider = GeminiTranscriptionProvider(
        "test-only-key",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TranscriptionProviderError) as error:
        await provider.transcribe(audio_path, "audio/flac")

    assert error.value.quota is True
    assert error.value.retry_after is not None
    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_gemini_temporary_file_is_deleted_when_transcription_fails(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "segment.flac"
    audio_path.write_bytes(b"test audio")
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/upload/v1beta/files":
            return httpx.Response(
                200,
                headers={
                    "X-Goog-Upload-URL": (
                        "https://generativelanguage.googleapis.com/upload/test-session"
                    )
                },
            )
        if request.url.path == "/upload/test-session":
            return httpx.Response(
                200,
                json={
                    "file": {
                        "name": "files/test-id",
                        "uri": "https://generativelanguage.googleapis.com/files/test-id",
                    }
                },
            )
        if request.url.path == "/v1beta/interactions":
            return httpx.Response(503)
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    provider = GeminiTranscriptionProvider(
        "test-only-key",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TranscriptionProviderError) as error:
        await provider.transcribe(audio_path, "audio/flac")

    assert error.value.transient is True
    assert methods == ["POST", "POST", "POST", "DELETE"]
