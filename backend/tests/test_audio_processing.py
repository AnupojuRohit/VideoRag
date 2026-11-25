from pathlib import Path

from app.services.ingestion.pipeline import (
    _make_transcript_chunks,
    reconcile_overlapping_words,
)
from app.services.media.audio import build_audio_segments


def test_audio_segments_stay_below_timestamp_limit_and_keep_offsets() -> None:
    segments = build_audio_segments(
        Path("audio.flac"),
        duration_seconds=3600,
        segment_seconds=1500,
        overlap_seconds=2,
    )

    assert [segment.start_seconds for segment in segments] == [0, 1498, 2996]
    assert [segment.duration_seconds for segment in segments] == [1500, 1500, 604]


def test_overlapping_segment_words_use_one_original_timeline_boundary() -> None:
    segments = [
        {"start_seconds": 0},
        {"start_seconds": 8},
    ]
    words = [
        [
            {"text": "first", "start_time": 7.5, "end_time": 7.8},
            {"text": "seam", "start_time": 8.9, "end_time": 9.2},
        ],
        [
            {"text": "seam", "start_time": 9.0, "end_time": 9.3},
            {"text": "next", "start_time": 10.2, "end_time": 10.5},
        ],
    ]

    reconciled = reconcile_overlapping_words(
        segments,
        words,
        overlap_seconds=2,
    )

    assert [word["text"] for word in reconciled] == ["first", "seam", "next"]
    assert reconciled[1]["start_time"] == 8.9


def test_transcript_chunks_keep_supported_original_time_bounds() -> None:
    chunks = _make_transcript_chunks(
        [
            {
                "text": "Redis",
                "start_time": 15.2,
                "end_time": 15.6,
                "speaker": "spk_1",
            },
            {
                "text": "works.",
                "start_time": 15.7,
                "end_time": 16.1,
                "speaker": "spk_1",
            },
        ]
    )

    assert len(chunks) == 1
    assert chunks[0]["content"] == "Redis works."
    assert chunks[0]["start_time"] == 15.2
    assert chunks[0]["end_time"] == 16.1
    assert chunks[0]["speakers"] == ["spk_1"]
