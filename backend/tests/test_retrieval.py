from uuid import UUID

import pytest
from pydantic import ValidationError

from app.models.enums import ChunkType
from app.schemas.retrieval import VideoSearchRequest
from app.services.retrieval import (
    EvidenceCandidate,
    assemble_context,
    fuse_ranked_results,
)


def _candidate(
    identifier: int,
    chunk_type: ChunkType,
    content: str,
    start_time: float,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        id=UUID(int=identifier),
        chunk_type=chunk_type,
        content=content,
        start_time=start_time,
        end_time=start_time + 1,
        source_ref=f"source:{identifier}",
    )


def test_rrf_merges_channels_deduplicates_and_is_deterministic() -> None:
    transcript = _candidate(1, ChunkType.TRANSCRIPT, "Redis consumer groups", 2)
    visual = _candidate(2, ChunkType.VISUAL, "Diagram of consumer groups", 4)
    keyword_only = _candidate(3, ChunkType.TRANSCRIPT, "Consumer groups", 8)

    first = fuse_ranked_results(
        [visual, transcript],
        [transcript, keyword_only],
        limit=10,
    )
    second = fuse_ranked_results(
        [visual, transcript],
        [transcript, keyword_only],
        limit=10,
    )

    assert first == second
    assert [item.id for item in first] == [transcript.id, visual.id, keyword_only.id]
    assert first[0].vector_rank == 2
    assert first[0].keyword_rank == 1
    assert first[1].vector_rank == 1
    assert first[1].keyword_rank is None


def test_context_assembly_keeps_modality_and_timestamps_under_limit() -> None:
    evidence = fuse_ranked_results(
        [
            _candidate(1, ChunkType.TRANSCRIPT, "The speaker explains RRF.", 12.5),
            _candidate(2, ChunkType.VISUAL, "A slide shows a ranking table.", 18),
        ],
        [],
        limit=2,
    )

    context, included = assemble_context(evidence, max_chars=500)

    assert len(context) <= 500
    assert "modality=speaker transcript" in context
    assert "timestamp=12.500-13.500" in context
    assert [item.citation_id for item in included] == ["E1", "E2"]

    too_small, none_included = assemble_context(evidence, max_chars=10)
    assert too_small == ""
    assert none_included == []


def test_search_request_rejects_empty_and_oversized_utf8_queries() -> None:
    with pytest.raises(ValidationError):
        VideoSearchRequest(query="   ")

    with pytest.raises(ValidationError):
        VideoSearchRequest(query="é" * 751)
