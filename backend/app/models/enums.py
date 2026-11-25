from enum import Enum


class LifecycleStatus(str, Enum):
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    INDEXING = "INDEXING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    PAUSED_QUOTA = "PAUSED_QUOTA"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    PAUSED_QUOTA = "PAUSED_QUOTA"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ChunkType(str, Enum):
    TRANSCRIPT = "TRANSCRIPT"
    VISUAL = "VISUAL"


PIPELINE_STAGES = (
    "media_validation",
    "media_preparation",
    "audio_extraction",
    "audio_segmentation",
    "transcription",
    "transcript_chunking",
    "scene_detection",
    "frame_extraction",
    "ocr",
    "visual_analysis",
    "embeddings",
    "indexing",
    "verification",
)
