from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
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
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: int = Field(default=30, gt=0)
    embedding_model_id: str | None = None
    embedding_model_version: str | None = None
    embedding_dimension: int | None = Field(default=None, gt=0)

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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
