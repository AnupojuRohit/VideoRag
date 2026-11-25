import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote, urlsplit

import aiofiles
import httpx

from app.core.config import get_settings
from app.providers.errors import TranscriptionProviderError
from app.providers.transcription import (
    ProviderUsage,
    TranscribedWord,
    TranscriptionResult,
    TranscriptionProvider,
)

logger = logging.getLogger(__name__)
MODEL_ID = "gemini-3.5-transcribe"
API_ROOT = "https://generativelanguage.googleapis.com"
_OFFSET_RE = re.compile(r"^(?P<seconds>\d+(?:\.\d+)?)s$")
_FILE_NAME_RE = re.compile(r"^files/[A-Za-z0-9_-]+$")
_CHUNK_SIZE = 1024 * 1024


def _retry_after(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(parsed, datetime.now(timezone.utc))
    return datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))


def _seconds(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp is missing")
    match = _OFFSET_RE.fullmatch(value)
    if match is None:
        raise ValueError("timestamp is not a seconds offset")
    return float(match.group("seconds"))


def _parse_words(body: dict[str, Any]) -> tuple[str, list[TranscribedWord]]:
    text = body.get("output_text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("transcription output text is missing")

    words: list[TranscribedWord] = []
    steps = body.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("interaction steps are invalid")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("interaction step is invalid")
        content_items = step.get("content", []) or []
        if not isinstance(content_items, list):
            raise ValueError("interaction content is invalid")
        for content in content_items:
            if not isinstance(content, dict):
                raise ValueError("interaction content item is invalid")
            annotations = content.get("annotations", []) or []
            if not isinstance(annotations, list):
                raise ValueError("interaction annotations are invalid")
            for annotation in annotations:
                if not isinstance(annotation, dict):
                    raise ValueError("word annotation is invalid")
                if annotation.get("type") != "word_info":
                    continue
                word_text = annotation.get("text")
                if not isinstance(word_text, str) or not word_text:
                    raise ValueError("word text is missing")
                start = _seconds(annotation.get("start_offset"))
                end = _seconds(annotation.get("end_offset"))
                if start < 0 or end < start:
                    raise ValueError("word timestamps are invalid")
                speaker = annotation.get("speaker")
                words.append(
                    TranscribedWord(
                        text=word_text,
                        start_seconds=start,
                        end_seconds=end,
                        speaker=speaker if isinstance(speaker, str) else None,
                    )
                )
    if not words:
        raise ValueError("word-level timestamp annotations are missing")
    return text, words


def _http_error(response: httpx.Response, operation: str) -> TranscriptionProviderError:
    status = response.status_code
    request_id = response.headers.get(
        "x-goog-request-id",
        response.headers.get("x-request-id"),
    )
    if status == 429:
        return TranscriptionProviderError(
            "provider_quota_exceeded",
            "The free transcription quota is temporarily unavailable.",
            retryable=True,
            transient=True,
            status_code=status,
            retry_after=_retry_after(response.headers.get("Retry-After")),
            quota=True,
            request_id=request_id,
        )
    if status >= 500 or status in {408, 425}:
        return TranscriptionProviderError(
            "provider_temporarily_unavailable",
            "The transcription provider is temporarily unavailable.",
            retryable=True,
            transient=True,
            status_code=status,
            retry_after=_retry_after(response.headers.get("Retry-After")),
            request_id=request_id,
        )
    if status in {401, 403}:
        return TranscriptionProviderError(
            "provider_authentication_failed",
            "The configured transcription provider credentials or project settings are not valid.",
            retryable=True,
            status_code=status,
            request_id=request_id,
        )
    code = "provider_upload_rejected" if operation == "file_upload" else "provider_request_rejected"
    return TranscriptionProviderError(
        code,
        "The transcription provider rejected this audio request.",
        retryable=False,
        status_code=status,
        request_id=request_id,
    )


class GeminiTranscriptionProvider:
    def __init__(
        self,
        api_key: str,
        timeout_seconds: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def _file_chunks(self, path: Path) -> AsyncIterator[bytes]:
        async with aiofiles.open(path, "rb") as audio_file:
            while chunk := await audio_file.read(_CHUNK_SIZE):
                yield chunk

    async def _start_upload(
        self,
        client: httpx.AsyncClient,
        audio_path: Path,
        mime_type: str,
    ) -> str:
        response = await client.post(
            f"{API_ROOT}/upload/v1beta/files",
            headers={
                "x-goog-api-key": self._api_key,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(audio_path.stat().st_size),
                "X-Goog-Upload-Header-Content-Type": mime_type,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": audio_path.name}},
        )
        if not response.is_success:
            raise _http_error(response, "file_upload")
        upload_url = response.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise TranscriptionProviderError(
                "provider_upload_protocol_error",
                "The transcription provider did not return an upload URL.",
                retryable=True,
                transient=True,
                status_code=response.status_code,
            )
        parsed_url = urlsplit(upload_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "generativelanguage.googleapis.com"
        ):
            raise TranscriptionProviderError(
                "provider_upload_protocol_error",
                "The transcription provider returned an unsafe upload URL.",
                retryable=False,
                status_code=response.status_code,
            )
        return upload_url

    async def _upload_file(
        self,
        client: httpx.AsyncClient,
        upload_url: str,
        audio_path: Path,
        mime_type: str,
    ) -> dict[str, Any]:
        response = await client.post(
            upload_url,
            headers={
                "x-goog-api-key": self._api_key,
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "Content-Type": mime_type,
                "Content-Length": str(audio_path.stat().st_size),
            },
            content=self._file_chunks(audio_path),
        )
        if not response.is_success:
            raise _http_error(response, "file_upload")
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise TranscriptionProviderError(
                "provider_upload_protocol_error",
                "The transcription provider returned an invalid upload response.",
                retryable=True,
                transient=True,
                status_code=response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise TranscriptionProviderError(
                "provider_upload_protocol_error",
                "The transcription provider returned an invalid upload response.",
                retryable=True,
                transient=True,
                status_code=response.status_code,
            )
        file = body.get("file")
        if not isinstance(file, dict) or not isinstance(file.get("uri"), str):
            raise TranscriptionProviderError(
                "provider_upload_protocol_error",
                "The transcription provider returned an incomplete upload response.",
                retryable=True,
                status_code=response.status_code,
            )
        name = file.get("name")
        if not isinstance(name, str) or not _FILE_NAME_RE.fullmatch(name):
            raise TranscriptionProviderError(
                "provider_upload_protocol_error",
                "The transcription provider returned an invalid file reference.",
                retryable=True,
                status_code=response.status_code,
            )
        return file

    async def _delete_file(
        self,
        client: httpx.AsyncClient,
        file_name: str,
    ) -> str | None:
        path = quote(file_name, safe="/")
        try:
            response = await client.delete(
                f"{API_ROOT}/v1beta/{path}",
                headers={"x-goog-api-key": self._api_key},
            )
        except httpx.HTTPError:
            return "provider_file_cleanup_failed"
        if response.is_success or response.status_code == 404:
            return None
        return "provider_file_cleanup_failed"

    async def transcribe(
        self,
        audio_path: Path,
        mime_type: str,
    ) -> TranscriptionResult:
        started = time.monotonic()
        file_name: str | None = None
        status_code: int | None = None
        interaction_id: str | None = None
        cleanup_error: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            ) as client:
                try:
                    upload_url = await self._start_upload(
                        client,
                        audio_path,
                        mime_type,
                    )
                    uploaded_file = await self._upload_file(
                        client,
                        upload_url,
                        audio_path,
                        mime_type,
                    )
                    file_name = uploaded_file["name"]
                    response = await client.post(
                        f"{API_ROOT}/v1beta/interactions",
                        headers={
                            "x-goog-api-key": self._api_key,
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": f"models/{MODEL_ID}",
                            "input": [
                                {
                                    "type": "file",
                                    "file_uri": uploaded_file["uri"],
                                }
                            ],
                            "generation_config": {
                                "transcription_config": {
                                    "mode": {
                                        "type": "verbatim",
                                        "timestamp_granularities": ["word"],
                                    }
                                }
                            },
                            "store": False,
                        },
                    )
                    status_code = response.status_code
                    if not response.is_success:
                        raise _http_error(response, "transcription")
                    try:
                        body = response.json()
                        if not isinstance(body, dict):
                            raise ValueError("interaction response is invalid")
                        interaction_id = body.get("id")
                        if body.get("status") != "completed":
                            raise ValueError("interaction did not complete")
                        transcript, words = _parse_words(body)
                    except (ValueError, TypeError, KeyError) as exc:
                        raise TranscriptionProviderError(
                            "provider_response_invalid",
                            "The transcription provider returned no valid word timestamps.",
                            retryable=False,
                            status_code=response.status_code,
                        ) from exc
                finally:
                    if file_name is not None:
                        cleanup_error = await self._delete_file(client, file_name)
        except TranscriptionProviderError as exc:
            exc.latency_ms = round((time.monotonic() - started) * 1000)
            raise
        except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
            provider_error = TranscriptionProviderError(
                "provider_network_error",
                "The transcription provider could not be reached.",
                retryable=True,
                transient=True,
                latency_ms=round((time.monotonic() - started) * 1000),
            )
            raise provider_error from exc
        except httpx.ProtocolError as exc:
            provider_error = TranscriptionProviderError(
                "provider_protocol_error",
                "The transcription provider returned an incomplete response.",
                retryable=True,
                transient=True,
                latency_ms=round((time.monotonic() - started) * 1000),
            )
            raise provider_error from exc

        if cleanup_error:
            logger.warning(
                "Temporary provider file could not be deleted immediately",
                extra={"provider": "gemini", "error_code": cleanup_error},
            )
        usage = ProviderUsage(
            provider="gemini",
            model_id=MODEL_ID,
            operation="transcription",
            status_code=status_code,
            latency_ms=round((time.monotonic() - started) * 1000),
            input_bytes=audio_path.stat().st_size,
            request_id=interaction_id if isinstance(interaction_id, str) else None,
            cleanup_error=cleanup_error,
        )
        return TranscriptionResult(text=transcript, words=words, usage=usage)


def create_transcription_provider() -> TranscriptionProvider:
    settings = get_settings()
    if (
        settings.gemini_api_key is None
        or not settings.gemini_api_key.get_secret_value().strip()
    ):
        raise TranscriptionProviderError(
            "transcription_provider_not_configured",
            "Transcription is unavailable until a verified free-tier Gemini API key is configured.",
            retryable=True,
        )
    return GeminiTranscriptionProvider(
        api_key=settings.gemini_api_key.get_secret_value(),
        timeout_seconds=settings.transcription_request_timeout_seconds,
    )
