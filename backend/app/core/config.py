from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "VideoRAG API"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = (
        "postgresql+asyncpg://videorag:videorag@localhost:5432/videorag"
    )
    redis_url: str = "redis://localhost:6379/0"
    frontend_origin: str = "http://localhost:3000"
    media_root: Path = Path("./data")
    max_upload_bytes: int = Field(default=500_000_000, gt=0)
    max_video_duration_seconds: float = Field(default=3600, gt=0)
    ffmpeg_path: str = "ffmpeg"
    ffmpeg_timeout_seconds: int = Field(default=1800, gt=0)
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: int = Field(default=30, gt=0)
    scene_detection_timeout_seconds: int = Field(default=1800, gt=0)
    scene_threshold: float = Field(default=27, gt=0, allow_inf_nan=False)
    frame_extraction_timeout_seconds: int = Field(default=60, gt=0)
    frame_sampling_interval_seconds: float = Field(
        default=15,
        gt=0,
        allow_inf_nan=False,
    )
    frame_min_spacing_seconds: float = Field(
        default=2,
        ge=0,
        allow_inf_nan=False,
    )
    max_frames_per_video: int = Field(default=120, gt=0, le=1000)
    frame_max_dimension: int = Field(default=1280, gt=0, le=4096)
    perceptual_hash_distance: int = Field(default=4, ge=0, le=64)
    tesseract_path: str = "tesseract"
    tesseract_language: str = "eng"
    ocr_timeout_seconds: int = Field(default=20, gt=0)
    vision_request_timeout_seconds: int = Field(default=180, gt=0)
    vision_max_image_bytes: int = Field(default=10_000_000, gt=0, le=14_000_000)
    audio_segment_seconds: float = Field(default=1500, gt=0, lt=1800)
    audio_segment_overlap_seconds: float = Field(default=2, ge=0, lt=30)
    transcription_request_timeout_seconds: int = Field(default=600, gt=0)
    job_lease_seconds: int = Field(default=90, gt=0)
    job_dispatch_cooldown_seconds: int = Field(default=60, gt=0)
    job_reconcile_interval_seconds: int = Field(default=15, gt=0)
    job_max_attempts: int = Field(default=5, gt=0)
    worker_concurrency: int = Field(default=2, gt=0)
    gemini_api_key: SecretStr | None = None
    gemini_unpaid_api_acknowledged: bool = False
    gemini_api_terms_acknowledged: bool = False
    gemini_region_eligible_acknowledged: bool = False
    embedding_model_id: str | None = "gemini-embedding-001"
    embedding_model_version: str | None = "stable"
    embedding_dimension: int | None = Field(default=768, gt=0)
    embedding_batch_size: int = Field(default=4, gt=0, le=16)
    gemini_min_request_interval_seconds: float = Field(
        default=1,
        ge=0,
        le=60,
        allow_inf_nan=False,
    )
    embedding_request_timeout_seconds: int = Field(default=120, gt=0)
    generation_request_timeout_seconds: int = Field(default=120, gt=0)
    generation_max_output_tokens: int = Field(default=512, gt=0, le=65_536)

    @model_validator(mode="after")
    def validate_embedding_configuration(self) -> "Settings":
        configured = (
            self.embedding_model_id,
            self.embedding_model_version,
            self.embedding_dimension,
        )
        if any(value is not None for value in configured) and any(
            value is None for value in configured
        ):
            raise ValueError(
                "Embedding model ID, version, and dimension must be set together"
            )
        if (
            self.embedding_model_id == "gemini-embedding-001"
            and self.embedding_dimension not in {768, 1536, 3072}
        ):
            raise ValueError(
                "gemini-embedding-001 output dimensions must be 768, 1536, or 3072"
            )
        if (
            self.gemini_api_key is not None
            and self.gemini_api_key.get_secret_value().strip()
            and (
                not self.gemini_unpaid_api_acknowledged
                or not self.gemini_api_terms_acknowledged
                or not self.gemini_region_eligible_acknowledged
            )
        ):
            raise ValueError(
                "Gemini API use requires acknowledgement of the unpaid-tier data "
                "terms, age and professional-use requirements, and a project "
                "without active billing, and confirmation that unpaid use is "
                "permitted in the deployment region"
            )
        if self.audio_segment_overlap_seconds >= self.audio_segment_seconds:
            raise ValueError(
                "Audio segment overlap must be shorter than the segment duration"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
