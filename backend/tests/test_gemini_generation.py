import json

import httpx
import pytest

from app.providers.errors import GenerationProviderError
from app.providers.gemini_generation import (
    API_URL,
    MODEL_ID,
    GeminiGenerationProvider,
)


@pytest.mark.asyncio
async def test_generation_request_is_stateless_and_uses_structured_output() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(await request.aread()))
        return httpx.Response(
            200,
            headers={"x-goog-request-id": "answer-request"},
            json={
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "answer": "The speaker explains RRF.",
                                        "abstain": False,
                                        "inference": False,
                                        "citations": ["E1"],
                                    }
                                ),
                            }
                        ],
                    }
                ],
                "usage": {
                    "total_input_tokens": 23,
                    "total_output_tokens": 10,
                },
            },
        )

    provider = GeminiGenerationProvider(
        "test-only-key",
        timeout_seconds=5,
        max_output_tokens=128,
        transport=httpx.MockTransport(handler),
    )
    result = await provider.generate_answer(
        "What is RRF?",
        "[E1] modality=speaker transcript; timestamp=4.000-5.000\nRRF fuses rankings.",
    )

    assert API_URL.endswith("/v1beta/interactions")
    assert len(requests) == 1
    body = requests[0]
    assert body["model"] == MODEL_ID
    assert body["store"] is False
    assert body["generation_config"] == {
        "max_output_tokens": 128,
        "thinking_level": "low",
    }
    assert body["response_format"]["mime_type"] == "application/json"
    assert body["system_instruction"]
    assert json.loads(body["input"]) == {
        "question": "What is RRF?",
        "evidence": "[E1] modality=speaker transcript; timestamp=4.000-5.000\n"
        "RRF fuses rankings.",
    }
    assert result.answer == "The speaker explains RRF."
    assert result.citations == ["E1"]
    assert result.usage.input_tokens == 23
    assert result.usage.output_tokens == 10
    assert result.usage.request_id == "answer-request"


@pytest.mark.asyncio
async def test_generation_rejects_malformed_answers_and_maps_quota() -> None:
    async def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "text",
                                "text": '{"answer":"x","abstain":false,'
                                '"inference":false,"citations":["not-an-evidence-id"]}',
                            }
                        ],
                    }
                ],
            },
        )

    provider = GeminiGenerationProvider(
        "test-only-key",
        timeout_seconds=5,
        max_output_tokens=128,
        transport=httpx.MockTransport(malformed),
    )
    with pytest.raises(GenerationProviderError) as error:
        await provider.generate_answer("Question?", "[E1] evidence")
    assert error.value.code == "provider_response_invalid"
    assert error.value.retryable is False

    async def quota(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    provider = GeminiGenerationProvider(
        "test-only-key",
        timeout_seconds=5,
        max_output_tokens=128,
        transport=httpx.MockTransport(quota),
    )
    with pytest.raises(GenerationProviderError) as error:
        await provider.generate_answer("Question?", "[E1] evidence")
    assert error.value.quota is True
    assert error.value.retry_after is not None


@pytest.mark.asyncio
async def test_generation_requires_nonempty_question_and_evidence() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = GeminiGenerationProvider(
        "test-only-key",
        timeout_seconds=5,
        max_output_tokens=128,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GenerationProviderError) as error:
        await provider.generate_answer("Question?", " ")
    assert error.value.code == "generation_input_invalid"
    assert calls == 0
