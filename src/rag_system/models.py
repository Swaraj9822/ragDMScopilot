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
    question: str = Field(min_length=1, max_length=10000)
    document_ids: list[str] | None = Field(default=None, max_length=50)


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    evidence_status: str
    trace_id: str


class CopilotQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=10000)
    include_sql: bool = False
    user_id: str | None = None


class CopilotDataSource(BaseModel):
    table: str
    columns: list[str]


class CopilotQueryResponse(BaseModel):
    answer: str
    mode: str
    evidence_status: str
    trace_id: str
    sql: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    data_sources: list[CopilotDataSource] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class UnifiedQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=10000)
    document_ids: list[str] | None = Field(default=None, max_length=50)
    include_sql: bool = False
    user_id: str | None = None


class UnifiedQueryResponse(BaseModel):
    answer: str
    route: str
    evidence_status: str
    trace_id: str
    citations: list[Citation] = Field(default_factory=list)
    sql: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    data_sources: list[CopilotDataSource] = Field(default_factory=list)
    routing_reasoning: str | None = None
