from dataclasses import dataclass
from typing import Literal, Protocol

from app.providers.transcription import ProviderUsage

EmbeddingTask = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    usage: ProviderUsage


class EmbeddingProvider(Protocol):
    async def embed_texts(
        self,
        texts: list[str],
        task_type: EmbeddingTask,
    ) -> EmbeddingResult: ...
