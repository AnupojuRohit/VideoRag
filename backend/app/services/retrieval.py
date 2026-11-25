import asyncio
from dataclasses import dataclass, replace
from typing import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal
from app.models.enums import ChunkType
from app.models.video_chunk import VideoChunk
from app.providers.embedding import EmbeddingProvider
from app.services.indexing.shared import embed_query
from app.services.media.audio import MediaProcessingError

RRF_CONSTANT = 60
DEFAULT_CONTEXT_CHAR_LIMIT = 12_000


@dataclass(frozen=True)
class EvidenceCandidate:
    id: UUID
    chunk_type: ChunkType
    content: str
    start_time: float
    end_time: float
    source_ref: str


@dataclass(frozen=True)
class RetrievedEvidence:
    id: UUID
    chunk_type: ChunkType
    content: str
    start_time: float
    end_time: float
    source_ref: str
    vector_rank: int | None
    keyword_rank: int | None
    rrf_score: float
    citation_id: str | None = None


@dataclass
class _FusedCandidate:
    candidate: EvidenceCandidate
    score: float = 0.0
    vector_rank: int | None = None
    keyword_rank: int | None = None


@dataclass(frozen=True)
class RetrievalResult:
    evidence: list[RetrievedEvidence]
    context: str


def fuse_ranked_results(
    vector_results: Sequence[EvidenceCandidate],
    keyword_results: Sequence[EvidenceCandidate],
    *,
    limit: int,
    rrf_constant: int = RRF_CONSTANT,
) -> list[RetrievedEvidence]:
    if limit <= 0 or rrf_constant <= 0:
        raise ValueError("limit and rrf_constant must be positive")

    fused: dict[UUID, _FusedCandidate] = {}
    for results, channel in (
        (vector_results, "vector"),
        (keyword_results, "keyword"),
    ):
        for rank, candidate in enumerate(results, start=1):
            entry = fused.setdefault(candidate.id, _FusedCandidate(candidate))
            if channel == "vector":
                entry.vector_rank = rank
            else:
                entry.keyword_rank = rank
            entry.score += 1 / (rrf_constant + rank)

    ranked = [
        RetrievedEvidence(
            id=entry.candidate.id,
            chunk_type=entry.candidate.chunk_type,
            content=entry.candidate.content,
            start_time=entry.candidate.start_time,
            end_time=entry.candidate.end_time,
            source_ref=entry.candidate.source_ref,
            vector_rank=entry.vector_rank,
            keyword_rank=entry.keyword_rank,
            rrf_score=entry.score,
        )
        for entry in fused.values()
    ]
    return sorted(
        ranked,
        key=lambda item: (
            -item.rrf_score,
            item.start_time,
            item.chunk_type.value,
            item.source_ref,
            str(item.id),
        ),
    )[:limit]


def assemble_context(
    evidence: Sequence[RetrievedEvidence],
    *,
    max_chars: int = DEFAULT_CONTEXT_CHAR_LIMIT,
) -> tuple[str, list[RetrievedEvidence]]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    blocks: list[str] = []
    included: list[RetrievedEvidence] = []
    current_chars = 0
    for index, item in enumerate(evidence, start=1):
        modality = (
            "speaker transcript"
            if item.chunk_type == ChunkType.TRANSCRIPT
            else "visible frame/OCR/visual description"
        )
        citation_id = f"E{index}"
        block = (
            f"[{citation_id}] modality={modality}; "
            f"timestamp={item.start_time:.3f}-{item.end_time:.3f}; "
            f"source={item.source_ref}\n{item.content}"
        )
        block_size = len(block) + (2 if blocks else 0)
        if current_chars + block_size > max_chars:
            break
        blocks.append(block)
        included.append(replace(item, citation_id=citation_id))
        current_chars += block_size
    return "\n\n".join(blocks), included


async def _semantic_search(
    video_id: UUID,
    version_id: UUID,
    vector: list[float],
    limit: int,
) -> list[EvidenceCandidate]:
    distance = VideoChunk.embedding.cosine_distance(vector)
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(VideoChunk)
                .where(
                    VideoChunk.video_id == video_id,
                    VideoChunk.embedding_index_version_id == version_id,
                    VideoChunk.embedding.is_not(None),
                )
                .order_by(
                    distance.asc(),
                    VideoChunk.start_time.asc(),
                    VideoChunk.id.asc(),
                )
                .limit(limit)
            )
        ).all()
    return [_candidate(row) for row in rows]


async def _keyword_search(
    video_id: UUID,
    version_id: UUID,
    query: str,
    limit: int,
) -> list[EvidenceCandidate]:
    ts_query = func.plainto_tsquery("simple", query)
    rank = func.ts_rank_cd(VideoChunk.search_vector, ts_query)
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(VideoChunk)
                .where(
                    VideoChunk.video_id == video_id,
                    VideoChunk.embedding_index_version_id == version_id,
                    VideoChunk.search_vector.op("@@")(ts_query),
                )
                .order_by(
                    rank.desc(),
                    VideoChunk.start_time.asc(),
                    VideoChunk.id.asc(),
                )
                .limit(limit)
            )
        ).all()
    return [_candidate(row) for row in rows]


def _candidate(chunk: VideoChunk) -> EvidenceCandidate:
    return EvidenceCandidate(
        id=chunk.id,
        chunk_type=chunk.chunk_type,
        content=chunk.content,
        start_time=chunk.start_time,
        end_time=chunk.end_time,
        source_ref=chunk.source_ref,
    )


async def retrieve_video_evidence(
    job_id: UUID,
    video_id: UUID,
    query: str,
    *,
    limit: int = 10,
    provider: EmbeddingProvider | None = None,
) -> RetrievalResult:
    if limit <= 0 or limit > 50:
        raise MediaProcessingError(
            "retrieval_limit_invalid",
            "The requested evidence limit is outside the supported range.",
            retryable=False,
        )
    version, query_vector = await embed_query(
        job_id,
        query,
        provider=provider,
    )
    if len(query_vector) != version.dimension:
        raise MediaProcessingError(
            "query_embedding_dimension_invalid",
            "The query embedding does not match the active evidence index.",
            retryable=False,
        )
    try:
        vector_results, keyword_results = await asyncio.gather(
            _semantic_search(video_id, version.id, query_vector, limit),
            _keyword_search(video_id, version.id, query, limit),
        )
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "retrieval_query_failed",
            "The evidence search could not be completed.",
            retryable=True,
            transient=True,
        ) from exc

    evidence = fuse_ranked_results(
        vector_results,
        keyword_results,
        limit=limit,
    )
    context, included_evidence = assemble_context(evidence)
    return RetrievalResult(evidence=included_evidence, context=context)
