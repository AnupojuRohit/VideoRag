from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TranscribedWord:
    text: str
    start_seconds: float
    end_seconds: float
    speaker: str | None = None


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    model_id: str
    operation: str
    status_code: int | None
    latency_ms: int
    input_bytes: int | None
    request_id: str | None
    cleanup_error: str | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    words: list[TranscribedWord]
    usage: ProviderUsage


class TranscriptionProvider(Protocol):
    async def transcribe(
        self,
        audio_path: Path,
        mime_type: str,
    ) -> TranscriptionResult: ...
