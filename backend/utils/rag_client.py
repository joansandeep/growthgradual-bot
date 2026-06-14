"""
RAG Service Client
==================
Thin async wrapper around the Paperly RAG Service running on HuggingFace Spaces.
URL: https://sandy31-paperly-rag-service.hf.space  (set via RAG_SERVICE_URL env)

Endpoints used:
  POST /index   — index extracted text from uploaded files
  POST /query   — retrieve grounded context for a chat question
  POST /report  — full-coverage retrieval for report generation
  DELETE /session/{id} — clean up on new chat
"""

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("rag_client")

RAG_URL = os.environ.get(
    "RAG_SERVICE_URL",
    "https://sandy31-paperly-rag-service.hf.space",
).rstrip("/")

# HF Spaces cold-start can be slow — generous timeout
_TIMEOUT = httpx.Timeout(connect=15, read=60, write=30, pool=10)


async def rag_index(session_id: str, documents: list[dict]) -> dict:
    """
    Index documents in the RAG service for a session.
    documents: [{ id, name, text, source_type?, file_type?, metadata? }]
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            res = await c.post(f"{RAG_URL}/index", json={
                "session_id": session_id,
                "documents":  documents,
            })
        if res.is_success:
            data = res.json()
            log.info("RAG index: session=%s chunks_added=%s total=%s",
                     session_id[:8], data.get("chunks_added"), data.get("total_chunks"))
            return data
        log.warning("RAG index HTTP %d: %s", res.status_code, res.text[:200])
        return {"error": f"HTTP {res.status_code}", "chunks_added": 0}
    except Exception as exc:
        log.warning("RAG index failed (non-critical): %s", exc)
        return {"error": str(exc), "chunks_added": 0}


async def rag_query(
    session_id: str,
    question:   str,
    top_k:      int   = 8,
    min_score:  float = 0.15,
    is_general: bool  = False,
) -> dict:
    """
    Retrieve grounded context + system prompt for a chat question.
    Returns dict with: system_prompt, context, retrieved, source_files, has_content
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            res = await c.post(f"{RAG_URL}/query", json={
                "session_id": session_id,
                "question":   question,
                "top_k":      top_k,
                "min_score":  min_score,
                "is_general": is_general,
            })
        if res.is_success:
            data = res.json()
            log.info("RAG query: session=%s retrieved=%s has_content=%s",
                     session_id[:8], data.get("retrieved"), data.get("has_content"))
            return data
        log.warning("RAG query HTTP %d: %s", res.status_code, res.text[:200])
        return {"has_content": False, "system_prompt": "", "context": "", "retrieved": 0, "source_files": []}
    except Exception as exc:
        log.warning("RAG query failed (non-critical): %s", exc)
        return {"has_content": False, "system_prompt": "", "context": "", "retrieved": 0, "source_files": []}


async def rag_report(
    session_id:  str,
    report_spec: str,
    report_type: str = "comprehensive",
) -> dict:
    """
    Full-coverage retrieval for report generation.
    Returns dict with: system_prompt, context, retrieved, source_files, has_content
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            res = await c.post(f"{RAG_URL}/report", json={
                "session_id":  session_id,
                "report_spec": report_spec,
                "report_type": report_type,
            })
        if res.is_success:
            data = res.json()
            log.info("RAG report: session=%s retrieved=%s", session_id[:8], data.get("retrieved"))
            return data
        log.warning("RAG report HTTP %d: %s", res.status_code, res.text[:200])
        return {"has_content": False, "system_prompt": "", "context": "", "retrieved": 0, "source_files": []}
    except Exception as exc:
        log.warning("RAG report failed (non-critical): %s", exc)
        return {"has_content": False, "system_prompt": "", "context": "", "retrieved": 0, "source_files": []}


async def rag_delete_session(session_id: str) -> None:
    """Clean up RAG index when user starts a new chat."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as c:
            await c.delete(f"{RAG_URL}/session/{session_id}")
        log.info("RAG session deleted: %s", session_id[:8])
    except Exception as exc:
        log.debug("RAG delete session (non-critical): %s", exc)


async def rag_health() -> bool:
    """Check if RAG service is reachable."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as c:
            res = await c.get(f"{RAG_URL}/health")
        return res.is_success
    except Exception:
        return False
