from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.enums import LifecycleStatus


def _lifecycle_enum() -> Enum:
    return Enum(
        LifecycleStatus,
        name="lifecycle_status",
        values_callable=lambda values: [item.value for item in values],
        validate_strings=True,
    )


class VideoAsset(Base):
    __tablename__ = "video_assets"
    __table_args__ = (
        CheckConstraint(
            "sha256_hash ~ '^[0-9a-f]{64}$'",
            name="ck_video_assets_sha256_format",
        ),
        CheckConstraint("file_size_bytes > 0", name="ck_video_assets_positive_size"),
        CheckConstraint(
            "duration_seconds > 0",
            name="ck_video_assets_positive_duration",
        ),
        CheckConstraint(
            "width IS NULL OR width > 0",
            name="ck_video_assets_positive_width",
        ),
        CheckConstraint(
            "height IS NULL OR height > 0",
            name="ck_video_assets_positive_height",
        ),
        Index("ix_video_assets_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    sha256_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    video_codec: Mapped[str] = mapped_column(String(64), nullable=False)
    audio_codec: Mapped[str | None] = mapped_column(String(64))
    audio_start_time: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_rate: Mapped[float | None] = mapped_column(Float)
    external_processing_consent: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    external_processing_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    external_processing_consent_version: Mapped[str | None] = mapped_column(
        String(64),
    )
    status: Mapped[LifecycleStatus] = mapped_column(
        _lifecycle_enum(),
        nullable=False,
        default=LifecycleStatus.UPLOADED,
        server_default=text("'UPLOADED'"),
    )
    pipeline_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="1",
        server_default=text("'1'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    jobs: Mapped[list["ProcessingJob"]] = relationship(
        back_populates="video",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    chunks: Mapped[list["VideoChunk"]] = relationship(
        back_populates="video",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
