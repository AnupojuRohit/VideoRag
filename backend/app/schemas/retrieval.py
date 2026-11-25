from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.enums import ChunkType


class VideoSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1500)
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized.encode("utf-8")) > 1500:
            raise ValueError("query must contain between 1 and 1500 UTF-8 bytes")
        return normalized


class VideoSearchEvidence(BaseModel):
    citation_id: str | None
    chunk_id: UUID
    chunk_type: ChunkType
    content: str
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    source_ref: str
    vector_rank: int | None = Field(default=None, ge=1)
    keyword_rank: int | None = Field(default=None, ge=1)
    rrf_score: float = Field(gt=0)


class VideoSearchResponse(BaseModel):
    video_id: UUID
    evidence: list[VideoSearchEvidence]
    context_char_count: int = Field(ge=0)


class VideoAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1500)
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized.encode("utf-8")) > 1500:
            raise ValueError("question must contain between 1 and 1500 UTF-8 bytes")
        return normalized


class AnswerCitation(BaseModel):
    citation_id: str
    chunk_id: UUID
    chunk_type: ChunkType
    content: str
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)


class VideoAnswerResponse(BaseModel):
    video_id: UUID
    answer: str
    abstained: bool
    inference: bool
    citations: list[AnswerCitation]
