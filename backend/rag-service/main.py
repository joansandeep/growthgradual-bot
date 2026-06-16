"""Paperly RAG Service v2 — FastAPI app."""

import os, logging, time
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [RAG] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("rag-service")

app = FastAPI(title="Paperly RAG Service v2", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

from rag_engine import RAGEngine
engine = RAGEngine()


# ── Models ────────────────────────────────────────────────────

class DocInput(BaseModel):
    id: str
    name: str
    text: str
    source_type: str = "file"
    file_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = {}

class IndexReq(BaseModel):
    session_id: str
    documents: List[DocInput]

class QueryReq(BaseModel):
    session_id: str
    question: str
    top_k: int = Field(default=8, ge=1, le=30)
    min_score: float = Field(default=0.15, ge=0.0, le=1.0)
    file_count: int = 1
    is_general: bool = False

class ReportReq(BaseModel):
    session_id: str
    report_spec: str          # what kind of report to generate
    report_type: str = "comprehensive"  # comprehensive | summary | comparison | technical


# ── Routes ────────────────────────────────────────────────────

@app.get("/ping")
@app.head("/ping")
async def ping():
    """Lightweight wake-up endpoint — keeps HF Space warm and confirms it's alive."""
    return {"pong": True, "model": engine.model_name}

@app.get("/health")
@app.head("/health")
async def health():
    sessions = engine.list_sessions()
    return {
        "service": "rag-service",
        "status": "ok",
        "port": int(os.getenv("RAG_PORT", 4005)),
        "model": engine.model_name,
        "active_sessions": len(sessions),
        "total_chunks": sum(
            engine.get_session_info(s).get("chunk_count", 0) for s in sessions
        ),
    }


@app.post("/index")
async def index_docs(req: IndexReq):
    t0 = time.time()
    try:
        result = engine.index(req.session_id, [d.dict() for d in req.documents])
        elapsed = round((time.time() - t0) * 1000)
        log.info(f"Indexed session={req.session_id[:8]} docs={len(req.documents)} "
                 f"chunks={result['chunks_added']} ({elapsed}ms)")
        return {**result, "session_id": req.session_id, "elapsed_ms": elapsed}
    except Exception as e:
        log.error(f"Index error: {e}")
        raise HTTPException(500, str(e))


@app.post("/query")
async def query(req: QueryReq):
    t0 = time.time()
    try:
        # Let rag_engine's scope detection handle top_k — only override if
        # caller explicitly set a high value or is_general is forced True
        top_k = req.top_k
        min_score = req.min_score

        if req.is_general:
            # Caller explicitly flagged as general — pull everything
            top_k = min(30, engine.get_session_info(req.session_id).get("chunk_count", 30))
            min_score = 0.0

        result = engine.retrieve(
            session_id=req.session_id,
            question=req.question,
            top_k=top_k,
            min_score=min_score,
        )
        elapsed = round((time.time() - t0) * 1000)
        log.info(f"Q&A session={req.session_id[:8]} chunks={result['retrieved']} "
                 f"found={result['has_content']} ({elapsed}ms)")
        return {
            **result,
            "session_id": req.session_id,
            "question": req.question,
            "grounded_system_prompt": result["system_prompt"],
            "retrieved_chunks": result["retrieved"],
            "elapsed_ms": elapsed,
        }
    except Exception as e:
        log.error(f"Query error: {e}")
        raise HTTPException(500, str(e))


@app.post("/report")
async def generate_report(req: ReportReq):
    """Full-coverage retrieval for report generation — no threshold filtering."""
    t0 = time.time()
    try:
        result = engine.retrieve_for_report(
            session_id=req.session_id,
            report_spec=req.report_spec,
            report_type=req.report_type,
        )
        elapsed = round((time.time() - t0) * 1000)
        log.info(f"Report session={req.session_id[:8]} chunks={result['retrieved']} ({elapsed}ms)")
        return {
            **result,
            "session_id": req.session_id,
            "report_spec": req.report_spec,
            "report_type": req.report_type,
            "grounded_system_prompt": result["system_prompt"],
            "retrieved_chunks": result["retrieved"],
            "elapsed_ms": elapsed,
        }
    except Exception as e:
        log.error(f"Report error: {e}")
        raise HTTPException(500, str(e))


@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    engine.delete_session(session_id)
    return {"success": True, "session_id": session_id}


@app.get("/session/{session_id}/info")
async def session_info(session_id: str):
    return engine.get_session_info(session_id)


if __name__ == "__main__":
    port = int(os.getenv("RAG_PORT", 4005))
    log.info(f"🐍 RAG Service v2 starting on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
