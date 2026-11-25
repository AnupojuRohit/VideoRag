from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.enums import LifecycleStatus
from app.models.video_asset import _lifecycle_enum


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint(
            "video_id",
            "idempotency_key",
            name="uq_processing_jobs_video_idempotency",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_processing_jobs_nonnegative_attempts",
        ),
        Index("ix_processing_jobs_status_created", "status", "created_at"),
        Index(
            "uq_processing_jobs_one_active_per_video",
            "video_id",
            unique=True,
            postgresql_where=text(
                "status IN ('UPLOADED', 'QUEUED', 'PROCESSING', "
                "'INDEXING', 'VERIFYING', 'PAUSED_QUOTA')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    video_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("video_assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[LifecycleStatus] = mapped_column(
        _lifecycle_enum(),
        nullable=False,
        default=LifecycleStatus.UPLOADED,
        server_default=text("'UPLOADED'"),
    )
    current_stage: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="initial-ingestion",
        server_default=text("'initial-ingestion'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    checkpoint: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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

    video: Mapped["VideoAsset"] = relationship(back_populates="jobs")
    stages: Mapped[list["ProcessingStage"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
