import asyncio
import json
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.models.embedding_index_version import EmbeddingIndexVersion
from app.models.enums import ChunkType
from app.models.provider_usage_event import ProviderUsageEvent
from app.models.video_chunk import VideoChunk
from app.providers.embedding import EmbeddingProvider
from app.providers.errors import EmbeddingProviderError, ProviderError
from app.providers.gemini_embedding import create_embedding_provider
from app.providers.transcription import ProviderUsage
from app.services.media.audio import MediaProcessingError

_MAX_CHUNK_BYTES = 1500
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchableChunk:
    chunk_type: ChunkType
    content: str
    start_time: float
    end_time: float
    source_ref: str
    metadata: dict[str, Any]


def _split_utf8(text_value: str, max_bytes: int) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text_value:
        byte_count = len(character.encode("utf-8"))
        if byte_count > max_bytes:
            raise MediaProcessingError(
                "evidence_character_invalid",
                "An evidence item contains a character that exceeds the safe input limit.",
                retryable=False,
            )
        if current and current_bytes + byte_count > max_bytes:
            parts.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += byte_count
    if current:
        parts.append("".join(current))
    return parts


def normalize_evidence(
    items: list[dict[str, Any]],
    chunk_type: ChunkType,
) -> list[SearchableChunk]:
    normalized: list[SearchableChunk] = []
    seen_refs: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise MediaProcessingError(
                "evidence_item_invalid",
                "The saved evidence contains an invalid item.",
                retryable=False,
            )
        source_ref = item.get("source_ref")
        content = item.get("content")
        metadata = item.get("metadata", {})
        try:
            start_time = float(item["start_time"])
            end_time = float(item["end_time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaProcessingError(
                "evidence_timestamp_invalid",
                "The saved evidence contains an invalid timestamp.",
                retryable=False,
            ) from exc
        if (
            not isinstance(source_ref, str)
            or not source_ref
            or len(source_ref) > 512
            or not isinstance(content, str)
            or not content.strip()
            or not isinstance(metadata, dict)
            or not math.isfinite(start_time + end_time)
            or start_time < 0
            or end_time < start_time
        ):
            raise MediaProcessingError(
                "evidence_item_invalid",
                "The saved evidence contains invalid content or timestamps.",
                retryable=False,
            )
        try:
            json.dumps(metadata, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise MediaProcessingError(
                "evidence_metadata_invalid",
                "The saved evidence metadata is invalid.",
                retryable=False,
            ) from exc
        parts = _split_utf8(content, _MAX_CHUNK_BYTES)
        if len(parts) > 1 and len(source_ref) > 480:
            raise MediaProcessingError(
                "evidence_source_ref_invalid",
                "A long evidence reference cannot be safely split for indexing.",
                retryable=False,
            )
        for index, part in enumerate(parts):
            part_metadata = dict(metadata)
            if len(parts) > 1:
                part_metadata.update(
                    {
                        "parent_source_ref": source_ref,
                        "content_part_index": index,
                        "content_part_count": len(parts),
                    }
                )
                part_ref = f"{source_ref}:part:{index:04d}"
            else:
                part_ref = source_ref
            if part_ref in seen_refs:
                raise MediaProcessingError(
                    "evidence_source_ref_duplicate",
                    "The evidence batch contains duplicate source references.",
                    retryable=False,
                )
            seen_refs.add(part_ref)
            normalized.append(
                SearchableChunk(
                    chunk_type=chunk_type,
                    content=part,
                    start_time=start_time,
                    end_time=end_time,
                    source_ref=part_ref,
                    metadata=part_metadata,
                )
            )
    return normalized


async def _active_index_version(
    *,
    create_if_missing: bool = True,
) -> EmbeddingIndexVersion | None:
    settings = get_settings()
    if (
        not settings.embedding_model_id
        or not settings.embedding_model_version
        or settings.embedding_dimension is None
    ):
        raise MediaProcessingError(
            "embedding_configuration_incomplete",
            "The embedding model, version, and dimension must be configured together.",
            retryable=False,
        )
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('videorag-active-embedding-index', 0))"
                )
            )
            version = await session.scalar(
                select(EmbeddingIndexVersion)
                .where(EmbeddingIndexVersion.is_active.is_(True))
                .with_for_update()
            )
            if version is not None:
                if (
                    version.model_id != settings.embedding_model_id
                    or version.model_version != settings.embedding_model_version
                    or version.dimension != settings.embedding_dimension
                ):
                    raise MediaProcessingError(
                        "embedding_index_version_mismatch",
                        "The configured embedding model differs from the active index; an explicit re-embedding migration is required.",
                        retryable=False,
                    )
                return version
            if not create_if_missing:
                return None
            version = EmbeddingIndexVersion(
                model_id=settings.embedding_model_id,
                model_version=settings.embedding_model_version,
                dimension=settings.embedding_dimension,
                is_active=True,
            )
            session.add(version)
            await session.flush()
            return version


async def persist_provider_usage(
    job_id: UUID,
    stage_name: str,
    usage: ProviderUsage,
) -> None:
    try:
        async with SessionLocal() as session:
            async with session.begin():
                session.add(
                    ProviderUsageEvent(
                        job_id=job_id,
                        stage_name=stage_name,
                        provider=usage.provider,
                        model_id=usage.model_id,
                        operation=usage.operation,
                        status_code=usage.status_code,
                        latency_ms=usage.latency_ms,
                        input_bytes=usage.input_bytes,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        request_id=usage.request_id,
                        error_code=usage.cleanup_error,
                    )
                )
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "provider_usage_write_failed",
            "Provider usage could not be recorded.",
            retryable=True,
            transient=True,
        ) from exc


async def persist_provider_error(
    job_id: UUID,
    error: ProviderError,
    input_bytes: int,
    *,
    stage_name: str,
    model_id: str,
    operation: str,
) -> None:
    try:
        async with SessionLocal() as session:
            async with session.begin():
                session.add(
                    ProviderUsageEvent(
                        job_id=job_id,
                        stage_name=stage_name,
                        provider="gemini",
                        model_id=model_id,
                        operation=operation,
                        status_code=error.status_code,
                        latency_ms=error.latency_ms or 0,
                        input_bytes=input_bytes,
                        request_id=error.request_id,
                        error_code=error.code,
                    )
                )
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "provider_usage_write_failed",
            "Provider usage could not be recorded.",
            retryable=True,
            transient=True,
        ) from exc


async def _upsert_chunks(
    video_id: UUID,
    version_id: UUID,
    chunks: list[SearchableChunk],
    vectors: list[list[float]],
) -> None:
    rows = [
        {
            "video_id": video_id,
            "chunk_type": chunk.chunk_type.value,
            "content": chunk.content,
            "start_time": chunk.start_time,
            "end_time": chunk.end_time,
            "source_ref": chunk.source_ref,
            "metadata_json": chunk.metadata,
            "embedding_index_version_id": version_id,
            "embedding": vector,
        }
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    statement = insert(VideoChunk).values(rows)
    statement = statement.on_conflict_do_update(
        constraint="uq_video_chunks_video_source",
        set_={
            "chunk_type": statement.excluded.chunk_type,
            "content": statement.excluded.content,
            "start_time": statement.excluded.start_time,
            "end_time": statement.excluded.end_time,
            "metadata": statement.excluded["metadata"],
            "embedding_index_version_id": (
                statement.excluded.embedding_index_version_id
            ),
            "embedding": statement.excluded.embedding,
        },
    )
    try:
        async with SessionLocal() as session:
            async with session.begin():
                await session.execute(statement)
    except IntegrityError as exc:
        raise MediaProcessingError(
            "chunk_index_constraint_failed",
            "The evidence could not be saved because it conflicts with index constraints.",
            retryable=False,
        ) from exc
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "chunk_index_write_failed",
            "The evidence index could not be written to PostgreSQL.",
            retryable=True,
            transient=True,
        ) from exc


@asynccontextmanager
async def gemini_provider_lock():
    connection = await engine.connect()
    acquired = False
    try:
        await connection.execute(
            text(
                "SELECT pg_advisory_lock("
                "hashtextextended('videorag-gemini-online-model-requests', 0))"
            )
        )
        await connection.commit()
        acquired = True
        yield
    finally:
        if acquired:
            try:
                await connection.execute(
                    text(
                        "SELECT pg_advisory_unlock("
                        "hashtextextended('videorag-gemini-online-model-requests', 0))"
                    )
                )
                await connection.commit()
            except SQLAlchemyError:
                logger.exception("Could not explicitly release Gemini provider lock")
        await connection.close()


async def _pending_chunks(
    video_id: UUID,
    version_id: UUID,
    chunks: list[SearchableChunk],
) -> list[SearchableChunk]:
    try:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(
                        VideoChunk.source_ref,
                        VideoChunk.content,
                        VideoChunk.start_time,
                        VideoChunk.end_time,
                        VideoChunk.metadata_json,
                        VideoChunk.embedding_index_version_id,
                    ).where(
                        VideoChunk.video_id == video_id,
                        VideoChunk.source_ref.in_(
                            [chunk.source_ref for chunk in chunks]
                        ),
                        VideoChunk.embedding.is_not(None),
                    )
                )
            ).all()
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "chunk_index_read_failed",
            "The existing evidence index could not be checked.",
            retryable=True,
            transient=True,
        ) from exc
    existing = {
        source_ref: (
            content,
            start_time,
            end_time,
            metadata,
            index_version_id,
        )
        for source_ref, content, start_time, end_time, metadata, index_version_id in rows
    }
    return [
        chunk
        for chunk in chunks
        if existing.get(chunk.source_ref)
        != (
            chunk.content,
            chunk.start_time,
            chunk.end_time,
            chunk.metadata,
            version_id,
        )
    ]


async def pace_gemini_request(model_id: str) -> None:
    interval = get_settings().gemini_min_request_interval_seconds
    if interval == 0:
        return
    try:
        async with SessionLocal() as session:
            last_request = await session.scalar(
                select(func.max(ProviderUsageEvent.created_at)).where(
                    ProviderUsageEvent.provider == "gemini",
                    ProviderUsageEvent.model_id == model_id,
                )
            )
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "provider_request_pacing_unavailable",
            "Provider request pacing could not be checked.",
            retryable=True,
            transient=True,
        ) from exc
    if last_request is not None:
        elapsed = (datetime.now(timezone.utc) - last_request).total_seconds()
        delay = interval - elapsed
        if delay > 0:
            await asyncio.sleep(delay)


async def index_evidence(
    job_id: UUID,
    video_id: UUID,
    chunk_type: ChunkType,
    evidence_items: list[dict[str, Any]],
    *,
    provider: EmbeddingProvider | None = None,
) -> int:
    all_chunks = normalize_evidence(evidence_items, chunk_type)
    if not all_chunks:
        return 0
    settings = get_settings()
    try:
        version = await _active_index_version()
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "embedding_index_version_unavailable",
            "The active embedding index could not be loaded.",
            retryable=True,
            transient=True,
        ) from exc
    if version is None:
        raise MediaProcessingError(
            "embedding_index_version_unavailable",
            "The active embedding index could not be loaded.",
            retryable=True,
            transient=True,
        )
    chunks = await _pending_chunks(video_id, version.id, all_chunks)
    if not chunks:
        return 0
    try:
        async with gemini_provider_lock():
            active_provider = provider or create_embedding_provider()
            for offset in range(0, len(chunks), settings.embedding_batch_size):
                batch = chunks[offset : offset + settings.embedding_batch_size]
                await pace_gemini_request(settings.embedding_model_id or "")
                texts = [chunk.content for chunk in batch]
                try:
                    result = await active_provider.embed_texts(
                        texts,
                        "RETRIEVAL_DOCUMENT",
                    )
                except EmbeddingProviderError as exc:
                    await persist_provider_error(
                        job_id,
                        exc,
                        sum(len(value.encode("utf-8")) for value in texts),
                        stage_name="embeddings",
                        model_id=settings.embedding_model_id or "unknown",
                        operation="embedding",
                    )
                    raise
                if len(result.vectors) != len(batch):
                    raise EmbeddingProviderError(
                        "provider_response_invalid",
                        "The embedding provider returned an incomplete batch.",
                        retryable=False,
                    )
                await persist_provider_usage(job_id, "embeddings", result.usage)
                await _upsert_chunks(
                    video_id,
                    version.id,
                    batch,
                    result.vectors,
                )
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "embedding_provider_lock_failed",
            "The embedding request could not be coordinated safely.",
            retryable=True,
            transient=True,
        ) from exc
    return len(chunks)


async def embed_query(
    job_id: UUID,
    query: str,
    *,
    provider: EmbeddingProvider | None = None,
) -> tuple[EmbeddingIndexVersion, list[float]]:
    if not query.strip() or len(query.encode("utf-8")) > _MAX_CHUNK_BYTES:
        raise MediaProcessingError(
            "query_input_invalid",
            "The search question is empty or exceeds the supported input size.",
            retryable=False,
        )
    settings = get_settings()
    try:
        version = await _active_index_version(create_if_missing=False)
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "embedding_index_version_unavailable",
            "The active embedding index could not be loaded.",
            retryable=True,
            transient=True,
        ) from exc
    if version is None:
        raise MediaProcessingError(
            "embedding_index_not_available",
            "No searchable embedding index is available yet.",
            retryable=False,
        )

    try:
        async with gemini_provider_lock():
            active_provider = provider or create_embedding_provider()
            await pace_gemini_request(settings.embedding_model_id or "")
            try:
                result = await active_provider.embed_texts(
                    [query],
                    "RETRIEVAL_QUERY",
                )
            except EmbeddingProviderError as exc:
                await persist_provider_error(
                    job_id,
                    exc,
                    len(query.encode("utf-8")),
                    stage_name="retrieval",
                    model_id=settings.embedding_model_id or "unknown",
                    operation="embedding",
                )
                raise
            if (
                len(result.vectors) != 1
                or len(result.vectors[0]) != version.dimension
                or any(not math.isfinite(value) for value in result.vectors[0])
            ):
                raise EmbeddingProviderError(
                    "provider_response_invalid",
                    "The embedding provider returned a query vector with an invalid shape.",
                    retryable=False,
                )
            await persist_provider_usage(job_id, "retrieval", result.usage)
            return version, result.vectors[0]
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "embedding_provider_lock_failed",
            "The embedding request could not be coordinated safely.",
            retryable=True,
            transient=True,
        ) from exc


async def prune_stale_evidence(
    video_id: UUID,
    chunk_type: ChunkType,
    evidence_items: list[dict[str, Any]],
) -> None:
    expected = normalize_evidence(evidence_items, chunk_type)
    expected_refs = {chunk.source_ref for chunk in expected}
    try:
        async with SessionLocal() as session:
            async with session.begin():
                statement = delete(VideoChunk).where(
                    VideoChunk.video_id == video_id,
                    VideoChunk.chunk_type == chunk_type,
                )
                if expected_refs:
                    statement = statement.where(
                        VideoChunk.source_ref.not_in(expected_refs)
                    )
                await session.execute(statement)
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "stale_evidence_cleanup_failed",
            "Obsolete evidence could not be removed from the search index.",
            retryable=True,
            transient=True,
        ) from exc


async def verify_video_index(
    video_id: UUID,
    expected: list[SearchableChunk],
) -> dict[str, int]:
    try:
        version = await _active_index_version()
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(
                        VideoChunk.source_ref,
                        VideoChunk.chunk_type,
                        VideoChunk.embedding_index_version_id,
                        func.vector_dims(VideoChunk.embedding),
                        func.length(VideoChunk.search_vector),
                    ).where(VideoChunk.video_id == video_id)
                )
            ).all()
    except SQLAlchemyError as exc:
        raise MediaProcessingError(
            "index_verification_unavailable",
            "The saved evidence index could not be verified.",
            retryable=True,
            transient=True,
        ) from exc
    if version is None:
        raise MediaProcessingError(
            "embedding_index_version_unavailable",
            "The active embedding index could not be loaded.",
            retryable=True,
            transient=True,
        )

    expected_keys = {
        (chunk.source_ref, chunk.chunk_type)
        for chunk in expected
    }
    indexed_keys = {
        (source_ref, chunk_type)
        for source_ref, chunk_type, _, _, _ in rows
    }
    if expected_keys != indexed_keys or len(rows) != len(expected_keys):
        raise MediaProcessingError(
            "index_verification_failed",
            "The evidence index is missing chunks or contains unexpected chunks.",
            retryable=True,
        )
    for _, _, version_id, dimension, searchable_nodes in rows:
        if (
            version_id != version.id
            or dimension != version.dimension
            or searchable_nodes <= 0
        ):
            raise MediaProcessingError(
                "index_verification_failed",
                "An evidence chunk is missing its configured vector or searchable text.",
                retryable=True,
            )
    return {
        "chunk_count": len(rows),
        "embedding_dimension": version.dimension,
    }
