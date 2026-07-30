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

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("rag_client")

RAG_URL = os.environ.get(
    "RAG_SERVICE_URL",
    "https://sandy31-paperly-rag-service.hf.space",
).rstrip("/")

# HF Spaces cold-start can take 60-90s to come up from a sleeping state.
# connect=15 was too tight for that (the TCP handshake itself can stall
# while the Space container is still booting) and read=60 was cutting it
# close too — both bumped with headroom below.
_TIMEOUT = httpx.Timeout(connect=30, read=90, write=90, pool=10)


def _exc_str(exc: Exception) -> str:
    """
    httpx's timeout exceptions (ConnectTimeout, ReadTimeout, PoolTimeout) are
    raised with NO message — str(exc) is just ''. Every "...failed: %s"
    log line hit by one of these ends up with nothing after the colon,
    which makes it impossible to tell a timeout apart from any other error
    at a glance. Always include the exception type so the log is never
    silently blank.
    """
    msg = str(exc)
    return f"{type(exc).__name__}: {msg}" if msg else f"{type(exc).__name__} (no message — likely a connect/read timeout, e.g. HF Space cold start)"


async def _wake_rag_service(max_wait: float = 100.0, poll_interval: float = 5.0) -> bool:
    """
    A sleeping HF Space's wake-up proxy will accept a TCP connection for the
    *real* request (e.g. /index) but not hand it to the container until the
    container has finished booting — so the real call just sits there and
    eventually hits a write/read timeout with nothing logged on either side
    (confirmed: the RAG service's own container log had no entry at all for
    a request that timed out from our side after 30+s).

    Rather than let every cold-start request burn its full write/read
    timeout budget blind, poll the cheap /ping endpoint first with a short
    per-attempt timeout. Each attempt is logged, so if this *is* a cold
    start you'll see it waking up in real time in this service's own logs
    instead of a single opaque timeout 30-90s later. Returns True once
    /ping responds, False if it never comes up within max_wait.
    """
    deadline = asyncio.get_event_loop().time() + max_wait
    attempt = 0
    while True:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=10, write=10, pool=10)) as c:
                res = await c.get(f"{RAG_URL}/ping")
            if res.is_success:
                if attempt > 1:
                    log.info("RAG service woke up after %d ping(s)", attempt)
                return True
        except Exception as exc:
            log.info("RAG wake-up ping %d failed (%s) — Space likely still booting", attempt, _exc_str(exc))
        if asyncio.get_event_loop().time() >= deadline:
            log.warning("RAG service did not wake within %.0fs after %d ping(s)", max_wait, attempt)
            return False
        await asyncio.sleep(poll_interval)


async def rag_index(session_id: str, documents: list[dict]) -> dict:
    """
    Index documents in the RAG service for a session.
    documents: [{ id, name, text, source_type?, file_type?, metadata? }]
    """
    await _wake_rag_service()
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
        log.warning("RAG index failed (non-critical): %s", _exc_str(exc))
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
    await _wake_rag_service()
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
        log.warning("RAG query failed (non-critical): %s", _exc_str(exc))
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
    await _wake_rag_service()
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
        log.warning("RAG report failed (non-critical): %s", _exc_str(exc))
        return {"has_content": False, "system_prompt": "", "context": "", "retrieved": 0, "source_files": []}


async def rag_delete_session(session_id: str) -> None:
    """Clean up RAG index when user starts a new chat."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as c:
            await c.delete(f"{RAG_URL}/session/{session_id}")
        log.info("RAG session deleted: %s", session_id[:8])
    except Exception as exc:
        log.debug("RAG delete session (non-critical): %s", _exc_str(exc))


async def rag_health() -> bool:
    """Check if RAG service is reachable."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as c:
            res = await c.get(f"{RAG_URL}/health")
        return res.is_success
    except Exception:
        return False
