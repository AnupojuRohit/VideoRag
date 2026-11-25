from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.enums import ChunkType


class VideoChunk(Base):
    __tablename__ = "video_chunks"
    __table_args__ = (
        UniqueConstraint(
            "video_id",
            "source_ref",
            name="uq_video_chunks_video_source",
        ),
        CheckConstraint(
            "start_time >= 0",
            name="ck_video_chunks_nonnegative_start",
        ),
        CheckConstraint(
            "end_time >= start_time",
            name="ck_video_chunks_ordered_times",
        ),
        Index("ix_video_chunks_video_time", "video_id", "start_time"),
        Index(
            "ix_video_chunks_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    video_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("video_assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_type: Mapped[ChunkType] = mapped_column(
        Enum(
            ChunkType,
            name="chunk_type",
            values_callable=lambda values: [item.value for item in values],
            validate_strings=True,
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    embedding_index_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("embedding_index_versions.id", ondelete="RESTRICT"),
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    video: Mapped["VideoAsset"] = relationship(back_populates="chunks")
    embedding_index_version: Mapped["EmbeddingIndexVersion | None"] = relationship(
        back_populates="chunks",
    )
