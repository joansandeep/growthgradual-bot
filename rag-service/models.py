"""Request/Response models for the RAG service."""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


# ── Index ─────────────────────────────────────────────────────────

class DocumentInput(BaseModel):
    id: str = Field(..., description="File/pasted-text ID")
    name: str = Field(..., description="Display name (filename or label)")
    text: str = Field(..., description="Full extracted text")
    source_type: str = Field(default="file", description="'file' or 'pasted'")
    file_type: Optional[str] = Field(default=None, description="pdf, docx, image")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    session_id: str
    documents: List[DocumentInput]


class IndexResponse(BaseModel):
    session_id: str
    chunks_added: int
    total_chunks: int
    elapsed_ms: int


# ── Query ─────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    session_id: str
    question: str
    top_k: int = Field(default=8, ge=1, le=30)
    min_score: float = Field(default=0.10, ge=0.0, le=1.0)
    file_count: int = Field(default=0, description="Number of files in session — used for single vs multi-doc mode")
    is_general: bool = Field(default=False, description="True when user asks a general/overview question")


class RetrievedChunk(BaseModel):
    text: str
    source_name: str
    source_type: str
    score: float
    chunk_index: int
    doc_id: str


class QueryResponse(BaseModel):
    session_id: str
    question: str
    context: str                     # raw context string for LLM
    grounded_system_prompt: str      # full grounded system prompt
    retrieved_chunks: int
    source_files: List[str]
    has_content: bool
    elapsed_ms: int


# ── Health ────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    service: str
    status: str
    port: int
    model: str
    active_sessions: int
    total_chunks: int


# ── Delete ────────────────────────────────────────────────────────

class DeleteRequest(BaseModel):
    session_id: str
