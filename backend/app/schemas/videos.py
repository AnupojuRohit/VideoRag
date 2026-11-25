import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import LifecycleStatus
from app.schemas.jobs import JobProgressResponse

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class PreflightRequest(BaseModel):
    sha256_hash: str = Field(min_length=64, max_length=64)

    @field_validator("sha256_hash")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("sha256_hash must be a 64-character hexadecimal digest")
        return normalized


class PreflightResponse(BaseModel):
    can_upload: bool
    duplicate_action: Literal[
        "upload",
        "reuse_uploaded",
        "attach_existing_job",
        "reuse_ready",
        "reuse_paused_quota",
        "retry_available",
    ]
    asset_id: UUID | None = None
    job_id: UUID | None = None
    status: LifecycleStatus | None = None
    retry_available: bool = False
    external_processing_consent: bool = False


class UploadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: UUID
    job_id: UUID | None
    sha256_hash: str
    original_filename: str
    file_size_bytes: int
    duration_seconds: float
    status: LifecycleStatus
    duplicate: bool
    duplicate_action: Literal[
        "uploaded",
        "reuse_uploaded",
        "attach_existing_job",
        "reuse_ready",
        "reuse_paused_quota",
        "retry_available",
    ]


class VideoLibraryItem(BaseModel):
    id: UUID
    original_filename: str
    file_size_bytes: int
    duration_seconds: float
    status: LifecycleStatus
    created_at: datetime
    updated_at: datetime


class VideoLibraryResponse(BaseModel):
    videos: list[VideoLibraryItem]
    limit: int
    offset: int


class VideoDetailResponse(VideoLibraryItem):
    mime_type: str
    video_codec: str
    audio_codec: str | None
    width: int
    height: int
    external_processing_consent: bool
    job: JobProgressResponse | None


class ErrorPayload(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorPayload
