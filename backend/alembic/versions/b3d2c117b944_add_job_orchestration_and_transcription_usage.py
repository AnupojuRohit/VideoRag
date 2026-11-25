"""add job orchestration and transcription usage fields

Revision ID: b3d2c117b944
Revises: ea5fbb1bed54
Create Date: 2026-11-25 19:28:24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b3d2c117b944"
down_revision: Union[str, None] = "ea5fbb1bed54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE stage_status ADD VALUE IF NOT EXISTS 'CANCELLED'")
    op.add_column(
        "video_assets",
        sa.Column("audio_start_time", sa.Float()),
    )
    op.add_column(
        "video_assets",
        sa.Column(
            "external_processing_consent",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "video_assets",
        sa.Column("external_processing_consent_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "video_assets",
        sa.Column("external_processing_consent_version", sa.String(length=64)),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("resume_after", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_processing_jobs_recovery",
        "processing_jobs",
        ["status", "last_enqueued_at", "lease_expires_at"],
    )
    op.create_table(
        "provider_usage_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("stage_name", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_bytes", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("request_id", sa.String(length=255)),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_usage_job_created",
        "provider_usage_events",
        ["job_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_usage_job_created", table_name="provider_usage_events")
    op.drop_table("provider_usage_events")
    op.drop_index("ix_processing_jobs_recovery", table_name="processing_jobs")
    op.drop_column("processing_jobs", "resume_after")
    op.drop_column("processing_jobs", "last_enqueued_at")
    op.drop_column("processing_jobs", "lease_expires_at")
    op.drop_column("processing_jobs", "heartbeat_at")
    op.drop_column("video_assets", "external_processing_consent_version")
    op.drop_column("video_assets", "external_processing_consent_at")
    op.drop_column("video_assets", "external_processing_consent")
    op.drop_column("video_assets", "audio_start_time")
    op.execute(
        "UPDATE processing_stages SET status = 'PENDING' WHERE status = 'CANCELLED'"
    )
    op.execute("ALTER TABLE processing_stages ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TYPE stage_status RENAME TO stage_status_with_cancelled"
    )
    op.execute(
        """
        CREATE TYPE stage_status AS ENUM (
            'PENDING', 'RUNNING', 'COMPLETED', 'SKIPPED', 'PAUSED_QUOTA', 'FAILED'
        )
        """
    )
    op.execute(
        """
        ALTER TABLE processing_stages
        ALTER COLUMN status TYPE stage_status
        USING status::text::stage_status
        """
    )
    op.execute(
        "ALTER TABLE processing_stages ALTER COLUMN status "
        "SET DEFAULT 'PENDING'::stage_status"
    )
    op.execute("DROP TYPE stage_status_with_cancelled")
