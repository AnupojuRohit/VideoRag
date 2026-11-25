from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.providers.transcription import ProviderUsage


@dataclass(frozen=True)
class VisionResult:
    description: str
    usage: ProviderUsage


class VisionProvider(Protocol):
    async def describe_frame(
        self,
        image_path: Path,
        ocr_text: str,
    ) -> VisionResult: ...
