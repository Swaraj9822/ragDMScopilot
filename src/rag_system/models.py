from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DocumentStatus(StrEnum):
    queued = "queued"
    parsing = "parsing"
    chunking = "chunking"
    embedding = "embedding"
    indexed = "indexed"
    failed = "failed"
    deleted = "deleted"


class DocumentRecord(BaseModel):
    id: str
    title: str
    version: str
    s3_uri: str
    status: DocumentStatus
    error: str | None = None


class ParsedDocument(BaseModel):
    document_id: str
    version: str
    markdown: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    id: str
    document_id: str
    version: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddedChunk(BaseModel):
    chunk: Chunk
    dense_vector: list[float]
    sparse_vector: dict[str, Any] | None = None


class Citation(BaseModel):
    document_id: str
    chunk_id: str
    page_start: int | None = None
    page_end: int | None = None
    title: str | None = None


class RetrievalHit(BaseModel):
    chunk: Chunk
    score: float
    source: str


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)
    document_ids: list[str] | None = None


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    evidence_status: str
    trace_id: str
    confidence: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    insufficient_evidence_reason: str | None = None


class CopilotQueryRequest(BaseModel):
    question: str = Field(min_length=1)
    include_sql: bool = False


class CopilotDataSource(BaseModel):
    table: str
    columns: list[str]


class CopilotQueryResponse(BaseModel):
    answer: str
    mode: str
    evidence_status: str
    trace_id: str
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    sql: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    data_sources: list[CopilotDataSource] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class UnifiedQueryRequest(BaseModel):
    question: str = Field(min_length=1)
    document_ids: list[str] | None = None
    include_sql: bool = False


class UnifiedQueryResponse(BaseModel):
    answer: str
    route: str
    evidence_status: str
    trace_id: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    insufficient_evidence_reason: str | None = None
    sql: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    data_sources: list[CopilotDataSource] = Field(default_factory=list)
    routing_reasoning: str | None = None


class QueryTraceHit(BaseModel):
    chunk_id: str
    document_id: str
    version: str
    score: float
    source: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    title: str | None = None
    section_path: list[str] = Field(default_factory=list)


class QueryTraceRecord(BaseModel):
    trace_id: str
    question: str
    route: str
    retrieval_mode: str | None = None
    document_ids: list[str] | None = None
    answer: str
    evidence_status: str
    confidence: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    insufficient_evidence_reason: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    retrieved_hits: list[QueryTraceHit] = Field(default_factory=list)
    model_ids: dict[str, str] = Field(default_factory=dict)
    latency_ms: float | None = None


class QueryFeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)
    expected_answer: str | None = Field(default=None, max_length=5000)


class QueryFeedbackRecord(QueryFeedbackRequest):
    trace_id: str
    feedback_id: str
    created_at: str

