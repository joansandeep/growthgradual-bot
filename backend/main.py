"""
Growth Gradual — In The Money  |  FastAPI Backend
Serves:
  POST /api/chat              — SSE streaming chat (Groq → Gemini fallback)
  POST /api/chat/report       — Multi-source research report (JSON)
  POST /api/chat/report/okf   — Open Knowledge Format bundle from report data (binary .zip)
  POST /api/chat/report/pdf   — PDF from report data (binary, legacy)
"""
import asyncio
import logging
import time
from pathlib import Path

# Load .env from backend directory — must happen before any route imports
try:
    from dotenv import load_dotenv
    _env = Path(__file__).parent / ".env"
    load_dotenv(_env if _env.exists() else None)
except ImportError:
    pass  # no dotenv installed — use real env vars (Render/prod)

# ─── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GG-Backend] %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from routes.chat import router as chat_router
from routes.report import router as report_router
from routes.pdf import router as pdf_router
from routes.okf import router as okf_router
from routes.email import router as email_router
from routes.rag import router as rag_router

app = FastAPI(title="Growth Gradual API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router,   prefix="/api/chat",                tags=["chat"])
app.include_router(report_router, prefix="/api/chat/report",         tags=["report"])
app.include_router(okf_router,    prefix="/api/chat/report/okf",     tags=["okf"])
app.include_router(pdf_router,    prefix="/api/chat/report/pdf",     tags=["pdf"])
app.include_router(email_router,  prefix="/api/chat/report/email",   tags=["email"])
app.include_router(rag_router,    prefix="/api/rag",                  tags=["rag"])


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    method = request.method
    path   = request.url.path
    log.info("→ %s %s", method, path)
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    log.info("← %s %s  %d  %.0fms", method, path, response.status_code, elapsed)
    return response


async def _keepalive_rag():
    """Ping the RAG service every 10 min to prevent HF Space cold-starts."""
    import os
    import httpx

    rag_url = os.environ.get(
        "RAG_SERVICE_URL", "https://sandy31-paperly-rag-service.hf.space"
    ).rstrip("/")
    ping_url = f"{rag_url}/ping"
    interval = 10 * 60  # seconds

    # Wait a bit after startup before first ping
    await asyncio.sleep(30)

    while True:
        try:
            # 15s was too tight for a Space that's actually cold (boot can take
            # 60-90s) — this ping's whole purpose is to catch that case, so it
            # needs to outlast it rather than time out and get logged as a
            # generic failure.
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=90)) as c:
                res = await c.get(ping_url)
            if res.is_success:
                log.info("RAG keepalive ✓  %s  →  %s", ping_url, res.json())
            else:
                log.warning("RAG keepalive HTTP %d", res.status_code)
        except Exception as exc:
            # httpx timeout exceptions carry no message (str(exc) == ''), so
            # bare "%s" % exc silently logs nothing after the colon. Always
            # include the exception type so a timeout is distinguishable from
            # a DNS failure, connection refusal, etc.
            msg = str(exc) or "no message — likely a connect/read timeout"
            log.warning("RAG keepalive failed: %s: %s", type(exc).__name__, msg)
        await asyncio.sleep(interval)


@app.on_event("startup")
async def on_startup():
    import os
    groq_n    = len([k for k in os.environ.get("GROQ_API_KEYS",    "").split(",") if k.strip()])
    tavily_n  = len([k for k in os.environ.get("TAVILY_API_KEY",   "").split(",") if k.strip()])
    gemini_n  = len([k for k in os.environ.get("GEMINI_API_KEY",   "").split(",") if k.strip()])
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  Growth Gradual — In The Money  |  Backend v1.0.0")
    log.info("  Groq keys: %d  |  Tavily keys: %d  |  Gemini keys: %d", groq_n, tavily_n, gemini_n)
    log.info("  Listening on http://0.0.0.0:8000")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    # Start background RAG keepalive (supplements external cron)
    asyncio.create_task(_keepalive_rag())
    log.info("  RAG keepalive task started (interval: 10 min)")


@app.get("/ping")
@app.head("/ping")
def ping():
    return {"pong": True, "service": "growth-gradual-backend"}


@app.get("/")
@app.head("/")
def root():
    return {"service": "Growth Gradual Backend", "status": "ok"}


@app.get("/health")
@app.head("/health")
def health():
    import os
    return {
        "status": "ok",
        "groq_keys":   len([k for k in os.environ.get("GROQ_API_KEYS",  "").split(",") if k.strip()]),
        "tavily_keys": len([k for k in os.environ.get("TAVILY_API_KEY", "").split(",") if k.strip()]),
        "gemini_keys": len([k for k in os.environ.get("GEMINI_API_KEY", "").split(",") if k.strip()]),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
