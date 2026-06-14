"""
POST /api/rag/index   — Index uploaded file text in the RAG service
POST /api/rag/query   — Query RAG for grounded context
DELETE /api/rag/session/{session_id} — Clean up session
GET  /api/rag/health  — Health check
"""
import logging
from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from utils.rag_client import rag_index, rag_query, rag_delete_session, rag_health

router = APIRouter()
log = logging.getLogger("rag_route")


@router.post("/index")
async def index_documents(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    session_id = body.get("session_id", "").strip()
    documents  = body.get("documents", [])

    if not session_id:
        return JSONResponse({"error": "session_id required"}, status_code=400)
    if not documents:
        return JSONResponse({"error": "documents list is empty"}, status_code=400)

    log.info("RAG /index: session=%s docs=%d", session_id[:8], len(documents))
    result = await rag_index(session_id, documents)
    return JSONResponse(result)


@router.post("/query")
async def query_rag(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    session_id = body.get("session_id", "").strip()
    question   = body.get("question", "").strip()
    top_k      = int(body.get("top_k", 8))
    min_score  = float(body.get("min_score", 0.15))
    is_general = bool(body.get("is_general", False))

    if not session_id or not question:
        return JSONResponse({"error": "session_id and question required"}, status_code=400)

    result = await rag_query(session_id, question, top_k, min_score, is_general)
    return JSONResponse(result)


@router.delete("/session/{session_id}")
async def delete_rag_session(session_id: str):
    await rag_delete_session(session_id)
    return JSONResponse({"success": True, "session_id": session_id})


@router.get("/health")
async def check_rag_health():
    ok = await rag_health()
    return JSONResponse({"rag_service": "ok" if ok else "unreachable", "url": "configured"})
