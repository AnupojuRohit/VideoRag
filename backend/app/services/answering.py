from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.providers.embedding import EmbeddingProvider
from app.providers.errors import GenerationProviderError
from app.providers.gemini_generation import MODEL_ID, create_generation_provider
from app.providers.generation import GenerationProvider
from app.services.indexing.shared import (
    gemini_provider_lock,
    pace_gemini_request,
    persist_provider_error,
    persist_provider_usage,
)
from app.services.media.audio import MediaProcessingError
from app.services.retrieval import RetrievedEvidence, retrieve_video_evidence

_ABSTENTION = "There is not enough evidence in this video to answer that question."


async def answer_video_question(
    job_id: UUID,
    video_id: UUID,
    question: str,
    *,
    limit: int = 10,
    embedding_provider: EmbeddingProvider | None = None,
    generation_provider: GenerationProvider | None = None,
) -> tuple[str, bool, list[RetrievedEvidence]]:
    retrieval = await retrieve_video_evidence(
        job_id,
        video_id,
        question,
        limit=limit,
        provider=embedding_provider,
    )
    if not retrieval.evidence or not retrieval.context:
        return _ABSTENTION, False, []

    provider = generation_provider or create_generation_provider()
    try:
        async with gemini_provider_lock():
            await pace_gemini_request(MODEL_ID)
            try:
                result = await provider.generate_answer(question, retrieval.context)
            except GenerationProviderError as error:
                await persist_provider_error(
                    job_id,
                    error,
                    len(question.encode("utf-8"))
                    + len(retrieval.context.encode("utf-8")),
                    stage_name="generation",
                    model_id=MODEL_ID,
                    operation="generation",
                )
                raise
            await persist_provider_usage(job_id, "generation", result.usage)
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "generation_provider_lock_failed",
            "The answer-generation request could not be coordinated safely.",
            retryable=True,
            transient=True,
        ) from exc

    if result.abstain or not result.answer.strip() or not result.citations:
        return _ABSTENTION, False, []

    evidence_by_id = {
        item.citation_id: item
        for item in retrieval.evidence
        if item.citation_id is not None
    }
    if any(citation not in evidence_by_id for citation in result.citations):
        error = GenerationProviderError(
            "provider_citation_invalid",
            "The answer-generation provider returned a citation that was not supplied as evidence.",
            retryable=False,
            status_code=result.usage.status_code,
            latency_ms=result.usage.latency_ms,
            request_id=result.usage.request_id,
        )
        await persist_provider_error(
            job_id,
            error,
            result.usage.input_bytes or 0,
            stage_name="generation",
            model_id=MODEL_ID,
            operation="generation",
        )
        raise error

    citations = [evidence_by_id[citation] for citation in result.citations]
    return result.answer.strip(), result.inference, citations
