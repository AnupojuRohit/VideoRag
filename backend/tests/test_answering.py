from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from app.models.enums import ChunkType
from app.providers.errors import GenerationProviderError
from app.providers.generation import GenerationResult
from app.providers.transcription import ProviderUsage
from app.services import answering
from app.services.retrieval import RetrievalResult, RetrievedEvidence


def _evidence(citation_id: str = "E1") -> RetrievedEvidence:
    return RetrievedEvidence(
        id=UUID(int=1),
        chunk_type=ChunkType.TRANSCRIPT,
        content="The speaker says RRF combines ranked lists.",
        start_time=12.5,
        end_time=16,
        source_ref="transcript:00001",
        vector_rank=1,
        keyword_rank=1,
        rrf_score=0.03,
        citation_id=citation_id,
    )


def _usage() -> ProviderUsage:
    return ProviderUsage(
        provider="gemini",
        model_id="gemini-3.8-flash",
        operation="generation",
        status_code=200,
        latency_ms=10,
        input_bytes=100,
        request_id="answer",
    )


@pytest.mark.asyncio
async def test_answer_uses_only_retrieved_citations_and_records_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def retrieve(*args, **kwargs) -> RetrievalResult:
        return RetrievalResult(
            evidence=[_evidence()],
            context="[E1] modality=speaker transcript; timestamp=12.500-16.000",
        )

    async def pace(_: str) -> None:
        return None

    async def persist_usage(*args) -> None:
        return None

    @asynccontextmanager
    async def lock():
        yield

    class FakeGenerationProvider:
        async def generate_answer(self, question: str, context: str) -> GenerationResult:
            assert question == "How is RRF described?"
            assert "speaker transcript" in context
            return GenerationResult(
                answer="RRF combines ranked lists.",
                abstain=False,
                inference=False,
                citations=["E1"],
                usage=_usage(),
            )

    monkeypatch.setattr(answering, "retrieve_video_evidence", retrieve)
    monkeypatch.setattr(answering, "pace_gemini_request", pace)
    monkeypatch.setattr(answering, "persist_provider_usage", persist_usage)
    monkeypatch.setattr(answering, "gemini_provider_lock", lock)

    answer, inference, citations = await answering.answer_video_question(
        UUID(int=2),
        UUID(int=3),
        "How is RRF described?",
        generation_provider=FakeGenerationProvider(),
    )

    assert answer == "RRF combines ranked lists."
    assert inference is False
    assert citations == [_evidence()]
    assert citations[0].start_time == 12.5


@pytest.mark.asyncio
async def test_answer_abstains_without_calling_generation_for_empty_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def retrieve(*args, **kwargs) -> RetrievalResult:
        return RetrievalResult(evidence=[], context="")

    class UnexpectedGenerationProvider:
        async def generate_answer(self, question: str, context: str) -> GenerationResult:
            raise AssertionError("generation must not run without retrieved evidence")

    monkeypatch.setattr(answering, "retrieve_video_evidence", retrieve)

    answer, inference, citations = await answering.answer_video_question(
        UUID(int=2),
        UUID(int=3),
        "What happened?",
        generation_provider=UnexpectedGenerationProvider(),
    )

    assert answer == answering._ABSTENTION
    assert inference is False
    assert citations == []


@pytest.mark.asyncio
async def test_answer_rejects_citations_not_in_retrieved_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def retrieve(*args, **kwargs) -> RetrievalResult:
        return RetrievalResult(evidence=[_evidence()], context="[E1] evidence")

    async def pace(_: str) -> None:
        return None

    async def persist(*args, **kwargs) -> None:
        return None

    @asynccontextmanager
    async def lock():
        yield

    class InvalidCitationProvider:
        async def generate_answer(self, question: str, context: str) -> GenerationResult:
            return GenerationResult(
                answer="Unsupported answer.",
                abstain=False,
                inference=False,
                citations=["E2"],
                usage=_usage(),
            )

    monkeypatch.setattr(answering, "retrieve_video_evidence", retrieve)
    monkeypatch.setattr(answering, "pace_gemini_request", pace)
    monkeypatch.setattr(answering, "persist_provider_usage", persist)
    monkeypatch.setattr(answering, "persist_provider_error", persist)
    monkeypatch.setattr(answering, "gemini_provider_lock", lock)

    with pytest.raises(GenerationProviderError) as error:
        await answering.answer_video_question(
            UUID(int=2),
            UUID(int=3),
            "Question?",
            generation_provider=InvalidCitationProvider(),
        )
    assert error.value.code == "provider_citation_invalid"
