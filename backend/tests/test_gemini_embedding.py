import json

import httpx
import pytest

from app.providers.errors import EmbeddingProviderError
from app.providers.gemini_embedding import (
    API_URL,
    MODEL_ID,
    GeminiEmbeddingProvider,
)


@pytest.mark.asyncio
async def test_embedding_request_batches_and_normalizes_vectors() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        requests.append(body)
        return httpx.Response(
            200,
            headers={"x-goog-request-id": "embedding-request"},
            json={
                "embeddings": [
                    {"values": [0.0, 3.0, 4.0]},
                    {"values": [1.0, 0.0, 0.0]},
                ],
                "usageMetadata": {"promptTokenCount": 11},
            },
        )

    provider = GeminiEmbeddingProvider(
        "test-only-key",
        timeout_seconds=5,
        dimension=3,
        batch_size=2,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.embed_texts(
        ["speech evidence", "visible frame evidence"],
        "RETRIEVAL_DOCUMENT",
    )

    assert len(requests) == 1
    assert requests[0]["requests"][0] == {
        "model": f"models/{MODEL_ID}",
        "content": {"parts": [{"text": "speech evidence"}]},
        "taskType": "RETRIEVAL_DOCUMENT",
        "outputDimensionality": 3,
    }
    assert result.vectors == [[0.0, 0.6, 0.8], [1.0, 0.0, 0.0]]
    assert result.usage.input_tokens == 11
    assert result.usage.request_id == "embedding-request"
    assert API_URL.endswith(":batchEmbedContents")


@pytest.mark.asyncio
async def test_embedding_provider_rejects_bad_vectors_and_quota(
) -> None:
    async def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [{"values": [1.0, 2.0]}]})

    provider = GeminiEmbeddingProvider(
        "test-only-key",
        timeout_seconds=5,
        dimension=3,
        batch_size=1,
        transport=httpx.MockTransport(malformed),
    )
    with pytest.raises(EmbeddingProviderError) as error:
        await provider.embed_texts(["evidence"], "RETRIEVAL_QUERY")
    assert error.value.code == "provider_response_invalid"

    async def quota(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    provider = GeminiEmbeddingProvider(
        "test-only-key",
        timeout_seconds=5,
        dimension=3,
        batch_size=1,
        transport=httpx.MockTransport(quota),
    )
    with pytest.raises(EmbeddingProviderError) as error:
        await provider.embed_texts(["evidence"], "RETRIEVAL_DOCUMENT")
    assert error.value.quota is True
    assert error.value.retry_after is not None


@pytest.mark.asyncio
async def test_embedding_provider_enforces_batch_and_byte_limits() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"embeddings": []})

    provider = GeminiEmbeddingProvider(
        "test-only-key",
        timeout_seconds=5,
        dimension=3,
        batch_size=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EmbeddingProviderError) as error:
        await provider.embed_texts(["x" * 1501], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_input_size_invalid"

    with pytest.raises(EmbeddingProviderError) as error:
        await provider.embed_texts(["first", "second"], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_batch_size_invalid"
    assert calls == 0
