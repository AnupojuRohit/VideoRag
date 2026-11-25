import json
import math
import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.providers.embedding import EmbeddingProvider, EmbeddingResult, EmbeddingTask
from app.providers.errors import EmbeddingProviderError
from app.providers.gemini_http import parse_retry_after, request_id
from app.providers.transcription import ProviderUsage

MODEL_ID = "gemini-embedding-001"
API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{MODEL_ID}:batchEmbedContents"
)
_MAX_TEXT_BYTES = 1500


def _token_count(body: dict[str, Any]) -> int | None:
    usage = body.get("usageMetadata")
    if not isinstance(usage, dict):
        return None
    value = usage.get("promptTokenCount")
    return value if isinstance(value, int) and value >= 0 else None


def _http_error(response: httpx.Response, started: float) -> EmbeddingProviderError:
    status = response.status_code
    common = {
        "status_code": status,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "request_id": request_id(response),
    }
    if status == 429:
        return EmbeddingProviderError(
            "provider_quota_exceeded",
            "The free embedding quota is temporarily unavailable.",
            retryable=True,
            transient=True,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
            quota=True,
            **common,
        )
    if status >= 500 or status in {408, 425}:
        return EmbeddingProviderError(
            "provider_temporarily_unavailable",
            "The embedding provider is temporarily unavailable.",
            retryable=True,
            transient=True,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
            **common,
        )
    if status in {401, 403}:
        return EmbeddingProviderError(
            "provider_authentication_failed",
            "The configured embedding provider credentials or project settings are not valid.",
            retryable=True,
            **common,
        )
    return EmbeddingProviderError(
        "embedding_request_rejected",
        "The embedding provider rejected the evidence batch.",
        retryable=False,
        **common,
    )


def _normalize(values: list[Any], expected_dimension: int) -> list[float]:
    if len(values) != expected_dimension:
        raise ValueError("embedding dimension does not match configuration")
    vector: list[float] = []
    for value in values:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("embedding contains a non-finite value")
        vector.append(float(value))
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0 or not math.isfinite(norm):
        raise ValueError("embedding vector has an invalid norm")
    return [value / norm for value in vector]


class GeminiEmbeddingProvider:
    def __init__(
        self,
        api_key: str,
        timeout_seconds: int,
        dimension: int,
        batch_size: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._dimension = dimension
        self._batch_size = batch_size
        self._transport = transport

    async def embed_texts(
        self,
        texts: list[str],
        task_type: EmbeddingTask,
    ) -> EmbeddingResult:
        if not texts or len(texts) > self._batch_size:
            raise EmbeddingProviderError(
                "embedding_batch_size_invalid",
                "The evidence batch exceeds the configured embedding limit.",
                retryable=False,
            )
        if any(
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8")) > _MAX_TEXT_BYTES
            for text in texts
        ):
            raise EmbeddingProviderError(
                "embedding_input_size_invalid",
                "An evidence item is empty or exceeds the safe embedding input limit.",
                retryable=False,
            )

        started = time.monotonic()
        request_body = {
            "requests": [
                {
                    "model": f"models/{MODEL_ID}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    "outputDimensionality": self._dimension,
                }
                for text in texts
            ]
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds, connect=10),
                transport=self._transport,
            ) as client:
                response = await client.post(
                    API_URL,
                    headers={
                        "x-goog-api-key": self._api_key,
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                )
        except httpx.TimeoutException as exc:
            raise EmbeddingProviderError(
                "provider_timeout",
                "The embedding provider did not respond within the configured time.",
                retryable=True,
                transient=True,
                latency_ms=round((time.monotonic() - started) * 1000),
            ) from exc
        except httpx.TransportError as exc:
            raise EmbeddingProviderError(
                "provider_connection_failed",
                "The embedding provider could not be reached.",
                retryable=True,
                transient=True,
                latency_ms=round((time.monotonic() - started) * 1000),
            ) from exc

        if not response.is_success:
            raise _http_error(response, started)
        try:
            body: Any = response.json()
        except json.JSONDecodeError as exc:
            raise EmbeddingProviderError(
                "provider_response_invalid",
                "The embedding provider returned an invalid response.",
                retryable=True,
                transient=True,
                status_code=response.status_code,
                latency_ms=round((time.monotonic() - started) * 1000),
            ) from exc
        embeddings = body.get("embeddings") if isinstance(body, dict) else None
        try:
            if not isinstance(embeddings, list) or len(embeddings) != len(texts):
                raise ValueError("embedding count does not match request count")
            vectors = [
                _normalize(embedding["values"], self._dimension)
                for embedding in embeddings
                if isinstance(embedding, dict)
            ]
            if len(vectors) != len(texts):
                raise ValueError("embedding item is invalid")
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingProviderError(
                "provider_response_invalid",
                "The embedding provider returned vectors with an invalid shape.",
                retryable=False,
                status_code=response.status_code,
                latency_ms=round((time.monotonic() - started) * 1000),
                request_id=request_id(response),
            ) from exc
        return EmbeddingResult(
            vectors=vectors,
            usage=ProviderUsage(
                provider="gemini",
                model_id=MODEL_ID,
                operation="embedding",
                status_code=response.status_code,
                latency_ms=round((time.monotonic() - started) * 1000),
                input_bytes=sum(len(text.encode("utf-8")) for text in texts),
                request_id=request_id(response),
                input_tokens=_token_count(body),
            ),
        )


def create_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    if settings.embedding_model_id != MODEL_ID:
        raise EmbeddingProviderError(
            "embedding_model_unsupported",
            "The configured embedding model has no verified provider adapter.",
            retryable=False,
        )
    if (
        settings.gemini_api_key is None
        or not settings.gemini_api_key.get_secret_value().strip()
    ):
        raise EmbeddingProviderError(
            "gemini_api_key_missing",
            "Configure GEMINI_API_KEY to enable online evidence embeddings.",
            retryable=True,
        )
    return GeminiEmbeddingProvider(
        api_key=settings.gemini_api_key.get_secret_value(),
        timeout_seconds=settings.embedding_request_timeout_seconds,
        dimension=settings.embedding_dimension or 768,
        batch_size=settings.embedding_batch_size,
    )
