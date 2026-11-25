import pytest

from app.models.enums import ChunkType
from app.services.indexing.shared import normalize_evidence
from app.services.media.audio import MediaProcessingError


def test_normalize_evidence_splits_utf8_without_exceeding_provider_limit() -> None:
    chunks = normalize_evidence(
        [
            {
                "source_ref": "transcript:00001",
                "content": "é" * 751,
                "start_time": 1.25,
                "end_time": 3.5,
                "metadata": {"speakers": ["speaker-1"]},
            }
        ],
        ChunkType.TRANSCRIPT,
    )

    assert [len(chunk.content.encode("utf-8")) for chunk in chunks] == [1500, 2]
    assert [chunk.source_ref for chunk in chunks] == [
        "transcript:00001:part:0000",
        "transcript:00001:part:0001",
    ]
    assert chunks[0].metadata["content_part_count"] == 2
    assert all(chunk.start_time == 1.25 and chunk.end_time == 3.5 for chunk in chunks)


def test_normalize_evidence_rejects_duplicate_source_refs() -> None:
    item = {
        "source_ref": "visual:frame:00001",
        "content": "A visible chart.",
        "start_time": 10,
        "end_time": 10,
        "metadata": {},
    }
    with pytest.raises(MediaProcessingError) as error:
        normalize_evidence([item, item], ChunkType.VISUAL)

    assert error.value.code == "evidence_source_ref_duplicate"
