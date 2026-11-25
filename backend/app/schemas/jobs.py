from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import LifecycleStatus, StageStatus


class StageProgress(BaseModel):
    name: str
    status: StageStatus
    attempt_count: int
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None


class JobProgressResponse(BaseModel):
    id: UUID
    video_id: UUID
    status: LifecycleStatus
    current_stage: str | None
    attempt_count: int
    completed_stages: int
    total_stages: int
    progress_percent: int
    started_at: datetime | None
    finished_at: datetime | None
    resume_after: datetime | None
    retry_available: bool
    cancel_available: bool
    stages: list[StageProgress]


class ProcessingConsentRequest(BaseModel):
    accepted: bool


class ProcessingConsentResponse(BaseModel):
    asset_id: UUID
    accepted: bool
    status: LifecycleStatus
    consent_version: str
