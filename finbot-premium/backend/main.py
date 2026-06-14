"""
Growth Gradual — In The Money  |  FastAPI Backend
Serves:
  POST /api/chat              — SSE streaming chat (Groq → Gemini fallback)
  POST /api/chat/report       — Multi-source research report (JSON)
  POST /api/chat/report/pdf   — PDF from report data (binary)
"""
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
from routes.email import router as email_router

app = FastAPI(title="Growth Gradual API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router,   prefix="/api/chat",                  tags=["chat"])
app.include_router(report_router, prefix="/api/chat/report",           tags=["report"])
app.include_router(pdf_router,    prefix="/api/chat/report/pdf",       tags=["pdf"])
app.include_router(email_router,  prefix="/api/chat/report/email",     tags=["email"])


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


@app.get("/health")
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
