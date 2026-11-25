from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import APIError
from app.models.enums import LifecycleStatus, PIPELINE_STAGES
from app.schemas.videos import PreflightRequest
from app.services.deduplication.service import action_for_status
from app.services.ingestion.uploads import sanitize_filename


def test_preflight_normalizes_valid_sha256() -> None:
    digest = "A" * 64

    request = PreflightRequest(sha256_hash=digest)

    assert request.sha256_hash == digest.lower()


@pytest.mark.parametrize(
    "digest",
    ["", "a" * 63, "g" * 64, "../" + "a" * 64],
)
def test_preflight_rejects_invalid_sha256(digest: str) -> None:
    with pytest.raises(ValidationError):
        PreflightRequest(sha256_hash=digest)


def test_filename_sanitization_uses_display_basename_only() -> None:
    assert sanitize_filename(r"..\private\lesson.mp4") == "lesson.mp4"
    assert sanitize_filename("lecture.mp4") == "lecture.mp4"


def test_filename_sanitization_rejects_other_extensions() -> None:
    with pytest.raises(APIError):
        sanitize_filename("lecture.mkv")


@pytest.mark.parametrize(
    ("status", "action"),
    [
        (LifecycleStatus.UPLOADED, "reuse_uploaded"),
        (LifecycleStatus.QUEUED, "attach_existing_job"),
        (LifecycleStatus.PROCESSING, "attach_existing_job"),
        (LifecycleStatus.INDEXING, "attach_existing_job"),
        (LifecycleStatus.VERIFYING, "attach_existing_job"),
        (LifecycleStatus.READY, "reuse_ready"),
        (LifecycleStatus.PAUSED_QUOTA, "reuse_paused_quota"),
        (LifecycleStatus.FAILED, "retry_available"),
        (LifecycleStatus.CANCELLED, "retry_available"),
    ],
)
def test_duplicate_actions_cover_asset_lifecycle(
    status: LifecycleStatus,
    action: str,
) -> None:
    assert action_for_status(status) == action


def test_embedding_settings_require_model_version_and_dimension_together() -> None:
    with pytest.raises(ValidationError):
        Settings(
            embedding_model_id="unverified-model",
            embedding_model_version=None,
            embedding_dimension=None,
        )

    settings = Settings(
        embedding_model_id="verified-model",
        embedding_model_version="v1",
        embedding_dimension=768,
    )
    assert settings.embedding_dimension == 768

    with pytest.raises(ValidationError):
        Settings(
            embedding_model_id="gemini-embedding-001",
            embedding_model_version="stable",
            embedding_dimension=3,
        )


def test_gemini_key_requires_unpaid_tier_terms_and_region_acknowledgements() -> None:
    with pytest.raises(ValidationError):
        Settings(gemini_api_key="test-only-key")

    with pytest.raises(ValidationError):
        Settings(
            gemini_api_key="test-only-key",
            gemini_unpaid_api_acknowledged=True,
        )

    with pytest.raises(ValidationError):
        Settings(
            gemini_api_key="test-only-key",
            gemini_unpaid_api_acknowledged=True,
            gemini_api_terms_acknowledged=True,
        )

    settings = Settings(
        gemini_api_key="test-only-key",
        gemini_unpaid_api_acknowledged=True,
        gemini_api_terms_acknowledged=True,
        gemini_region_eligible_acknowledged=True,
    )
    assert settings.gemini_api_key is not None


def test_pipeline_stage_names_are_unique() -> None:
    assert len(PIPELINE_STAGES) == len(set(PIPELINE_STAGES))
    assert {"media_validation", "audio_extraction", "visual_analysis"} <= set(
        PIPELINE_STAGES
    )
