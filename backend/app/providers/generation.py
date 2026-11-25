from dataclasses import dataclass
from typing import Protocol

from app.providers.transcription import ProviderUsage


@dataclass(frozen=True)
class GenerationResult:
    answer: str
    abstain: bool
    inference: bool
    citations: list[str]
    usage: ProviderUsage


class GenerationProvider(Protocol):
    async def generate_answer(
        self,
        question: str,
        evidence_context: str,
    ) -> GenerationResult: ...
