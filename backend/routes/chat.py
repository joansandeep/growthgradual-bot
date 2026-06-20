"""
POST /api/chat  — SSE streaming chat
Body: { messages: [{role, content}], fileContext?: string }
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import AsyncGenerator

import httpx
from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse, StreamingResponse

from utils.keys import (
    get_gemini_keys, get_groq_keys, get_tavily_keys,
    is_rate_limited, mark_rate_limited, pick_key, round_robin
)

router = APIRouter()
log = logging.getLogger("chat")
from datetime import datetime as _dt
from utils.rag_client import rag_query as _rag_query

# ─── Supabase persistence ──────────────────────────────────────────────────────
_SUPABASE_URL  = os.environ.get("SUPABASE_URL", "").rstrip("/")
_SUPABASE_KEY  = os.environ.get("SUPABASE_ANON_KEY", "")

def _sb_headers() -> dict:
    return {
        "apikey": _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

async def _upsert_session(session_id: str) -> None:
    """Insert session row if absent, otherwise bump last_active."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            # Try upsert first
            r = await client.post(
                f"{_SUPABASE_URL}/rest/v1/sessions",
                headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                json={"id": session_id, "last_active": _dt.utcnow().isoformat(), "query_count": 1},
            )
            if r.status_code in (401, 403):
                log.debug("Supabase sessions: RLS blocked insert (table may need policy) — skipping")
                return
            # Bump last_active
            await client.patch(
                f"{_SUPABASE_URL}/rest/v1/sessions",
                headers=_sb_headers(),
                params={"id": f"eq.{session_id}"},
                json={"last_active": _dt.utcnow().isoformat()},
            )
    except Exception as exc:
        log.debug("Supabase upsert_session failed (non-critical): %s", exc)


async def _save_messages(session_id: str, user_content: str, assistant_content: str,
                         llm_provider: str, elapsed_ms: int) -> None:
    """Persist user + assistant messages into the messages table."""
    if not _SUPABASE_URL or not _SUPABASE_KEY or not session_id:
        return
    rows = [
        {
            "session_id": session_id,
            "role": "user",
            "content": user_content,
            "llm_provider": llm_provider,
            "tokens_used": 0,
            "response_ms": 0,
        },
        {
            "session_id": session_id,
            "role": "assistant",
            "content": assistant_content,
            "llm_provider": llm_provider,
            "tokens_used": 0,
            "response_ms": elapsed_ms,
        },
    ]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{_SUPABASE_URL}/rest/v1/messages",
                headers=_sb_headers(),
                json=rows,
            )
        log.info("Supabase: saved user+assistant messages for session %s", session_id)
    except Exception as exc:
        log.warning("Supabase save_messages failed: %s", exc)


def _image_hash(data: str) -> str:
    """SHA-256 of the raw base64 string — stable fingerprint for dedup."""
    return hashlib.sha256(data.encode()).hexdigest()


async def _load_cached_extractions(session_id: str, img_hashes: list[str]) -> dict[str, str]:
    """
    Query the existing `files` table for rows matching this session + hash.
    Returns {file_hash: extracted_text} for any already-processed images.
    Falls back to cross-session lookup so the same image is never re-processed
    even across different sessions (e.g. user re-uploads same chart).
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY or not img_hashes:
        return {}
    try:
        hashes_csv = "(" + ",".join(f'"{h}"' for h in img_hashes) + ")"
        async with httpx.AsyncClient(timeout=8) as client:
            # 1. Try session-scoped lookup first (fastest, most relevant)
            params: dict = {
                "file_hash": f"in.{hashes_csv}",
                "select": "file_hash,extracted_text",
                "extracted_text": "neq.",          # non-empty
            }
            if session_id:
                params["session_id"] = f"eq.{session_id}"

            r = await client.get(
                f"{_SUPABASE_URL}/rest/v1/files",
                headers={**_sb_headers(), "Prefer": "return=representation"},
                params=params,
            )
            if r.is_success and r.json():
                result = {row["file_hash"]: row["extracted_text"]
                          for row in r.json() if row.get("extracted_text")}
                if result:
                    log.info("Supabase image cache: %d/%d hit(s) for session %s",
                             len(result), len(img_hashes), (session_id or "?")[:8])
                    return result

            # 2. Cross-session fallback — same image uploaded in any session
            if session_id:
                r2 = await client.get(
                    f"{_SUPABASE_URL}/rest/v1/files",
                    headers={**_sb_headers(), "Prefer": "return=representation"},
                    params={
                        "file_hash": f"in.{hashes_csv}",
                        "select": "file_hash,extracted_text",
                        "extracted_text": "neq.",
                        "limit": "10",
                    },
                )
                if r2.is_success and r2.json():
                    result = {row["file_hash"]: row["extracted_text"]
                              for row in r2.json() if row.get("extracted_text")}
                    if result:
                        log.info("Supabase image cache: %d/%d cross-session hit(s)",
                                 len(result), len(img_hashes))
                    return result
    except Exception as exc:
        log.debug("Supabase _load_cached_extractions failed: %s", exc)
    return {}


async def _store_image_extractions(
    session_id: str,
    images: list[dict],
    extractions: dict[str, str],
) -> None:
    """
    Persist newly extracted image text back into the existing `files` table
    (upsert on file_hash) and upload raw bytes to the `paperly-uploads` bucket
    so the file-service and RAG pipeline can also see them.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return

    async with httpx.AsyncClient(timeout=30) as client:
        for img in images:
            h = _image_hash(img["data"])
            text = extractions.get(h, "")
            if not text:
                continue

            mime  = img.get("mimeType", "image/jpeg")
            fname = img.get("name", f"{h[:8]}.jpg")
            ext   = mime.split("/")[-1].replace("jpeg", "jpg")
            bucket_path = f"{session_id}/{h}.{ext}" if session_id else f"chat-images/{h}.{ext}"

            # 1. Upload raw image to paperly-uploads bucket (x-upsert so safe to repeat)
            try:
                raw_bytes = base64.b64decode(img["data"])
                up = await client.post(
                    f"{_SUPABASE_URL}/storage/v1/object/paperly-uploads/{bucket_path}",
                    headers={
                        "apikey": _SUPABASE_KEY,
                        "Authorization": f"Bearer {_SUPABASE_KEY}",
                        "Content-Type": mime,
                        "x-upsert": "true",
                    },
                    content=raw_bytes,
                )
                stored_url = (
                    f"{_SUPABASE_URL}/storage/v1/object/public/paperly-uploads/{bucket_path}"
                    if up.is_success else fname
                )
                if up.is_success:
                    log.info("Supabase Storage: image saved → paperly-uploads/%s", bucket_path)
                else:
                    log.debug("Supabase Storage upload %d: %s", up.status_code, up.text[:80])
            except Exception as exc:
                log.debug("Supabase Storage upload error: %s", exc)
                stored_url = fname
                raw_bytes  = b""

            # 2. Upsert into files table (reuses existing schema — no migration needed)
            if session_id:
                try:
                    row = {
                        "session_id":    session_id,
                        "original_name": fname,
                        "stored_name":   stored_url,
                        "mime_type":     mime,
                        "file_type":     "image",
                        "size_bytes":    len(raw_bytes),
                        "extracted_text": text,
                        "has_text":      True,
                        "ocr_processed": True,
                        "word_count":    len(text.split()),
                        "file_hash":     h,
                    }
                    ins = await client.post(
                        f"{_SUPABASE_URL}/rest/v1/files",
                        headers={**_sb_headers(),
                                 "Prefer": "resolution=merge-duplicates,return=minimal"},
                        json=row,
                    )
                    if ins.is_success:
                        log.info("Supabase files: upserted image record hash=%s session=%s",
                                 h[:8], session_id[:8])
                    else:
                        log.warning("Supabase files upsert %d: %s",
                                    ins.status_code, ins.text[:200])
                except Exception as exc:
                    log.warning("Supabase _store_image_extractions upsert failed: %s", exc)

# ─── Domain lists ──────────────────────────────────────────────────────────────
FINANCE_DOMAINS = [
    # Indian finance — reliable scrapers
    "economictimes.indiatimes.com", "livemint.com", "business-standard.com",
    "ndtvprofit.com", "cnbctv18.com", "financialexpress.com",
    "thehindubusinessline.com", "zeebiz.com", "outlookbusiness.com",
    "moneycontrol.com", "bseindia.com", "nseindia.com",
    "screener.in", "tickertape.in", "equitymaster.com",
    "cafemutual.com", "amfiindia.com", "capitalmarket.com",
    # MF / SIP data
    "groww.in", "etmoney.com", "paytmmoney.com", "kuvera.in",
    "mfuindia.com", "advisorkhoj.com",
    # Global reliable
    "reuters.com", "cnbc.com", "marketwatch.com",
    "investing.com", "tradingeconomics.com",
    "rbi.org.in", "sebi.gov.in",
    "forbes.com", "businessinsider.com",
]

GENERAL_DOMAINS = [
    "reuters.com", "apnews.com", "bbc.com", "theguardian.com", "nytimes.com",
    "thehindu.com", "ndtv.com", "hindustantimes.com", "timesofindia.indiatimes.com",
    "indianexpress.com", "scroll.in", "thewire.in", "livemint.com",
    "techcrunch.com", "theverge.com", "wired.com", "arstechnica.com",
    "scientificamerican.com", "nature.com", "wikipedia.org",
    "forbes.com", "businessinsider.com", "economist.com",
]

FINANCE_TERMS = [
    # Indices & exchanges
    "nifty", "sensex", "bse", "nse", "ipo", "rbi", "sebi", "amfi",
    "nifty 50", "nifty bank", "nifty it", "nifty auto", "nifty fmcg",
    # Core finance words
    "stock", "share", "equity", "fund", "mutual fund", "nav", "sip",
    "market", "trading", "invest", "portfolio", "dividend", "earnings",
    "quarter", "q1", "q2", "q3", "q4", "fy", "balance sheet", "revenue",
    "profit", "loss", "ebitda", "pe ratio", "p/e", "eps", "book value",
    "rupee", "inr", "forex", "crude", "gold", "silver", "commodity",
    "inflation", "gdp", "repo rate", "monetary policy", "cpi", "wpi",
    "bank", "nbfc", "loan", "emi", "interest rate", "credit", "debit",
    # Companies
    "lic", "hdfc", "icici", "sbi", "axis", "kotak", "reliance", "tata",
    "infosys", "wipro", "tcs", "adani", "bajaj", "zerodha", "groww",
    "paytm", "zomato", "ola", "swiggy", "nykaa", "delhivery",
    # Analysis & report trigger words
    "sector", "performance", "analysis", "outlook", "forecast", "report",
    "returns", "rally", "correction", "bearish", "bullish", "momentum",
    "valuation", "fundamental", "technical", "breakout", "support", "resistance",
    "52 week", "all time high", "ath", "volume", "liquidity", "fii", "dii",
    "inflow", "outflow", "net buy", "net sell", "derivative", "futures", "options",
    "fno", "f&o", "expiry", "index", "benchmark", "mid cap", "small cap", "large cap",
    "bluechip", "penny stock", "etf", "reit", "aif", "pms", "demat",
    "ipo allotment", "listing", "grey market", "gmp", "buyback", "split",
    "bonus", "rights issue", "qip", "ofs", "block deal", "bulk deal",
    "results", "quarterly", "annual", "fy25", "fy26", "capex", "debt",
    "leverage", "margin", "return on equity", "roe", "roce", "cash flow",
    "npa", "provision", "slippage", "credit growth", "deposit",
    # Macro
    "rate cut", "rate hike", "policy", "budget", "fiscal", "trade deficit",
    "current account", "fdi", "fpi", "rupee depreciation", "dollar",
    "yield", "bond", "gilt", "treasury", "g-sec",
    # News triggers
    "latest", "today", "news", "update", "this week", "this month",
    "recent", "current", "now", "live", "trend",
]

SKIP_SEARCH_PREFIXES = [
    "what is ", "define ", "explain how ", "how does ",
    "what are the basics", "difference between",
]

# Phrases that always need a web search regardless of length
ALWAYS_SEARCH_PATTERNS = [
    r"\b(latest|recent|current|today|now|this week|this month|live)\b",
    r"\b(news|update|report|analysis|outlook|performance|returns?|rally|correction)\b",
    r"\b(sector|market|stock|nifty|sensex|bse|nse|ipo|rbi|sebi)\b",
    r"\b(result|quarter|earnings|profit|revenue|forecast|prediction)\b",
    r"\b(price|rate|yield|index|fii|dii|inflow|outflow)\b",
]

SKIP_PAGE_FETCH = [".pdf", "bloomberg.com", "wsj.com", "ft.com", "economist.com"]


# ─── Helpers ───────────────────────────────────────────────────────────────────

# Pronouns / references that signal a follow-up needing rewrite
_FOLLOWUP_RE = re.compile(
    r"\b(it|its|their|they|them|that|this|those|these|he|she|him|her|the company|"
    r"the stock|the fund|the bank|the same|above|mentioned|previous)\b",
    re.IGNORECASE,
)

async def rewrite_query_for_search(last_msg: str, history: list[dict]) -> str:
    """
    If `last_msg` looks like a pronoun-heavy follow-up, use the last 4 turns
    of conversation history to rewrite it into a self-contained search query.
    Falls back to the original message on any error.
    """
    # Only rewrite if it's short AND contains ambiguous references
    if len(last_msg) > 180 or not _FOLLOWUP_RE.search(last_msg):
        return last_msg

    # Build a compact context from the last 4 turns (2 user + 2 assistant)
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-4:]
    if not recent:
        return last_msg

    context_lines = "\n".join(
        f"{m['role'].upper()}: {str(m['content'])[:300]}" for m in recent
    )

    prompt = (
        "You are a search query rewriter for a financial assistant. "
        "Given the recent conversation and a follow-up message, rewrite the follow-up "
        "into a concise, self-contained web search query (max 10 words). "
        "Output ONLY the rewritten query — no explanation, no quotes, no punctuation at the end.\n\n"
        f"CONVERSATION:\n{context_lines}\n\n"
        f"FOLLOW-UP: {last_msg}\n\n"
        "REWRITTEN QUERY:"
    )

    # Use Groq (fast, cheap) for this lightweight rewrite task
    keys = get_groq_keys()
    if not keys:
        return last_msg

    key = keys[0]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=15, write=5, pool=5)) as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 30,
                    "temperature": 0.1,
                    "stream": False,
                },
            )
        if res.is_success:
            rewritten = res.json()["choices"][0]["message"]["content"].strip().strip('"').strip("'")
            if rewritten and len(rewritten) > 3:
                log.info("Query rewrite: %r → %r", last_msg[:60], rewritten[:80])
                return rewritten
    except Exception as exc:
        log.debug("Query rewrite failed (non-critical): %s", exc)

    return last_msg


def classify_query(msg: str) -> str:
    m = msg.lower()
    return "finance" if any(t in m for t in FINANCE_TERMS) else "general"


_FILE_INTENT_RE = re.compile(
    r"""\b(explain|summaris[e]?|summariz[e]?|describe|analys[e]?|analyz[e]?|read|
    translate|what.s|tell\sme|extract|find|list|show|convert|interpret|transcribe|
    what\s(can|do)\syou\s(see|say)|ocr|what\sdoes\sit\ssay)\b""",
    re.IGNORECASE | re.VERBOSE,
)

# Greetings and self-introductions — "hi", "hey", "hi i am albin", "hello my
# name is priya", "hi i am albin, nice to meet you" — none of these need a web
# search or a data-heavy persona. Uses fullmatch so a greeting attached to an
# actual question ("hi can you tell me the latest nifty level") is correctly
# treated as a real query, not smalltalk.
_SMALLTALK_RE = re.compile(
    r"""^\s*
    (hi+|hey+|hello+|yo+|hiya|sup|good\s?(morning|afternoon|evening|day)|namaste|howdy)
    \s*[,!.\-]?\s*
    (
        (i\s*(a|')?m|i\s+am|my\s+name\s+is|this\s+is|here)\s+
        [a-zA-Z][a-zA-Z'\-]*(\s+[a-zA-Z][a-zA-Z'\-]*){0,2}
    )?
    \s*[,!.\-]*\s*
    (nice\s+to\s+meet\s+you|there|all|everyone)?
    \s*[!.]*\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Pure self-introductions with no greeting word at all — "my name is albin",
# "this is albin" — also count as smalltalk.
_SELF_INTRO_RE = re.compile(
    r"^\s*(my\s+name\s+is|i\s*(a|')?m|i\s+am|this\s+is)\s+"
    r"[a-zA-Z][a-zA-Z'\-]*(\s+[a-zA-Z][a-zA-Z'\-]*){0,2}\s*[.!]?\s*$",
    re.IGNORECASE,
)


def is_smalltalk(msg: str) -> bool:
    """True for greetings / self-introductions that deserve a short, simple
    reply rather than a full data-driven analyst response — e.g. 'hi',
    'hi i am albin', 'hello, my name is priya'. Uses fullmatch so it never
    fires on a real question that merely starts with a greeting word."""
    m = msg.strip()
    if not m or len(m) > 60:
        return False
    return bool(_SMALLTALK_RE.fullmatch(m) or _SELF_INTRO_RE.fullmatch(m))


def needs_web_search(msg: str, has_files: bool = False) -> bool:
    m = msg.lower().strip()
    if len(m) < 4:
        return False
    TRIVIAL = {"hi", "hey", "hello", "ok", "okay", "thanks", "thank you", "bye", "yes", "no", "sure", "great"}
    if m in TRIVIAL or is_smalltalk(msg):
        return False
    # If files are attached, skip web search for file-directed queries
    if has_files:
        if len(m) <= 80:
            return False
        if _FILE_INTENT_RE.search(m):
            return False
    return True


async def fetch_page_content(url: str, max_chars: int = 4000) -> str:
    if any(s in url for s in SKIP_PAGE_FETCH):
        return ""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            }, follow_redirects=True)
            if not res.is_success:
                return ""
            html = res.text
            clean = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
            clean = re.sub(r"<style[\s\S]*?</style>", "", clean, flags=re.IGNORECASE)
            clean = re.sub(r"<[^>]+>", " ", clean)
            clean = re.sub(r"\s{3,}", "\n", clean)
            for esc, rep in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")]:
                clean = clean.replace(esc, rep)
            return clean.strip()[:max_chars]
    except Exception as exc:
        log.debug("fetch_page_content failed for %s: %s", url, exc)
        return ""


_AI_OVERVIEW_PATTERNS = [
    r"^\s*\*{0,2}as of [a-z]+,?\s+[a-z]+\s+\d{1,2},?\s+\d{4}.{0,40}here are the latest",
    r"^\s*\*{0,2}latest .{0,60}\*{0,2}\s*as of\b",
    r"^\s*here(?:'s| is) (?:a |an )?(?:summary|overview|update) of",
]

def _looks_like_ai_overview(text: str) -> bool:
    if not text:
        return False
    head = text.strip()[:300].lower()
    return any(re.search(p, head, re.IGNORECASE) for p in _AI_OVERVIEW_PATTERNS)


def _clean_result_content(r: dict) -> dict:
    """Drop snippet/fullContent fields that look like injected AI-overview text."""
    if _looks_like_ai_overview(r.get("snippet", "")):
        r["snippet"] = ""
    if _looks_like_ai_overview(r.get("fullContent", "")):
        r["fullContent"] = ""
    return r


async def tavily_search(query: str, max_results: int = 20, min_results: int = 10) -> list[dict]:
    """
    Search via the official tavily-python SDK.

    Key params used:
      search_depth="advanced"     — deeper crawl, more content per source
      chunks_per_source=3         — up to 3 content chunks per page (SDK feature)
      include_answer="advanced"   — pre-synthesized answer injected as result[0]
      include_raw_content=True    — full page text (not just snippet)
      country="india"             — biases Tavily's ranking toward Indian sources
      topic="finance"             — activates Tavily's finance-optimised index
      max_results=20              — Tavily plan cap; playground confirms 20 max_results;
                                    asking for more wastes quota without extra results (verified in API Playground)
    """
    keys = get_tavily_keys()
    if not keys:
        log.warning("Tavily search skipped — no keys configured")
        return []

    qtype = classify_query(query)
    domains = FINANCE_DOMAINS if qtype == "finance" else GENERAL_DOMAINS
    log.info("Tavily search (SDK): query=%r  type=%s  max_results=%d", query[:60], qtype, max_results)
    t0 = time.perf_counter()

    # Import here so the module works even if tavily-python isn't installed yet
    try:
        from tavily import AsyncTavilyClient
    except ImportError:
        log.warning("tavily-python not installed — falling back to httpx REST call")
        return await _tavily_search_httpx_fallback(query, max_results, domains, qtype)

    for key in round_robin(keys):
        if is_rate_limited(key):
            continue
        try:
            client = AsyncTavilyClient(api_key=key)

            search_kwargs: dict = {
                "query": query,
                "search_depth": "advanced",
                "max_results": max_results,          # reliable cap for all plan tiers
                "chunks_per_source": 3,              # 3 content chunks per page
                "include_answer": "advanced",        # synthesized answer as bonus context
                "include_raw_content": True,         # full page text
                "include_images": False,             # not needed for text chat
                "include_image_descriptions": False,
                "include_favicon": False,
                "country": "india",                  # bias toward Indian sources
                "include_domains": domains,
            }
            if qtype == "finance":
                search_kwargs["topic"] = "finance"   # Tavily finance index

            data = await client.search(**search_kwargs)

            results: list[dict] = []

            # Tavily SDK returns a dict with "results" list + optional "answer"
            raw_results = data.get("results") or []

            # NOTE: Tavily's synthesized "answer" field is intentionally NOT added
            # as a virtual result here. It used to be appended with title
            # "Tavily Answer: ..." and url "https://tavily.com", which the report
            # LLM would then cite as if it were a real publication (e.g.
            # "[1] Tavily. Top banking stocks. https://tavily.com" showing up in
            # the References section). Tavily is our internal search provider,
            # not a citable source, so we drop the synthesized answer and rely
            # on the individual real results below for substance.

            for r in raw_results:
                # chunks_per_source gives us r["chunks"] — concatenate them
                chunks = r.get("chunks") or []
                chunk_text = "\n\n".join(
                    c.get("content", "") for c in chunks if c.get("content")
                )
                raw_content = (
                    chunk_text
                    or r.get("raw_content")
                    or r.get("content")
                    or ""
                )
                results.append({
                    "title":       r.get("title", ""),
                    "url":         r.get("url", ""),
                    "snippet":     r.get("content", "")[:800],
                    "fullContent": raw_content[:5000],
                    "score":       r.get("score"),
                    "published":   r.get("published_date"),
                })

            results = [_clean_result_content(r) for r in results]

            if len(results) < min_results:
                log.info(
                    "Tavily SDK: %d results (target min=%d) — keeping domain filter",
                    len(results), min_results,
                )

            # Enrich results that still have thin content (< 500 chars)
            async def _passthrough(val: str) -> str:
                return val

            enrich_tasks = [
                fetch_page_content(r["url"], 3000)
                if len(r.get("fullContent", "")) < 500 else _passthrough(r.get("fullContent", ""))
                for r in results
            ]
            extra_contents = await asyncio.gather(*enrich_tasks, return_exceptions=True)
            for i, r in enumerate(results):
                extra = extra_contents[i] if not isinstance(extra_contents[i], Exception) else ""
                if isinstance(extra, str) and len(extra) > len(r.get("fullContent", "")):
                    r["fullContent"] = extra

            elapsed = (time.perf_counter() - t0) * 1000
            log.info("Tavily SDK done: %d results in %.0fms", len(results), elapsed)
            return results

        except Exception as exc:
            err_str = str(exc).lower()
            if "429" in err_str or "rate" in err_str:
                log.warning("Tavily SDK 429/rate-limit on key ...%s — backing off 60s", key[-4:])
                mark_rate_limited(key, 60_000)
            elif "401" in err_str or "403" in err_str or "invalid" in err_str:
                log.warning("Tavily SDK auth error on key ...%s — banning 24h", key[-4:])
                mark_rate_limited(key, 24 * 60 * 60_000)
            else:
                log.warning("Tavily SDK exception key ...%s: %s", key[-4:], exc)
            continue

    log.error("Tavily: all keys exhausted or failed — trying httpx fallback")
    return await _tavily_search_httpx_fallback(query, max_results, domains, qtype)


async def _tavily_search_httpx_fallback(
    query: str, max_results: int, domains: list[str], qtype: str
) -> list[dict]:
    """Raw httpx fallback if tavily-python SDK is unavailable."""
    keys = get_tavily_keys()
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=15) as client:
        for key in round_robin(keys):
            if is_rate_limited(key):
                continue
            try:
                body: dict = {
                    "api_key": key,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": True,
                    "include_domains": domains,
                    "country": "india",
                }
                if qtype == "finance":
                    body["topic"] = "finance"
                res = await client.post("https://api.tavily.com/search", json=body)
                if res.status_code == 429:
                    mark_rate_limited(key, 60_000)
                    continue
                if res.status_code in (401, 403):
                    mark_rate_limited(key, 24 * 60 * 60_000)
                    continue
                if not res.is_success:
                    continue
                data = res.json()
                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("content", ""),
                        "fullContent": (r.get("raw_content") or r.get("content") or "")[:5000],
                        "score": r.get("score"),
                        "published": r.get("published_date"),
                    }
                    for r in (data.get("results") or [])
                ]
                elapsed = (time.perf_counter() - t0) * 1000
                log.info("Tavily httpx fallback: %d results in %.0fms", len(results), elapsed)
                return [_clean_result_content(r) for r in results]
            except Exception as exc:
                log.warning("Tavily httpx fallback exception: %s", exc)
                continue
    return []


async def load_headlines(limit: int = 30) -> str:
    candidates = [
        Path(os.getcwd()) / "growth_gradual_cache.json",
        Path("/tmp/growth_gradual_cache.json"),
    ]
    raw = None
    for p in candidates:
        try:
            raw = p.read_text(encoding="utf-8")
            log.debug("Headlines loaded from %s", p)
            break
        except Exception:
            pass
    if not raw:
        log.debug("No headline cache found — skipping headlines injection")
        return ""
    try:
        data = json.loads(raw)
        articles = (data.get("articles") or [])[:limit]
        if not articles:
            return ""
        from datetime import datetime, timezone
        fetched_ts = data.get("fetchedAt")
        try:
            fetched_dt = datetime.fromtimestamp(fetched_ts / 1000, tz=timezone.utc).strftime("%d %b %Y, %I:%M %p")
        except Exception:
            fetched_dt = "recently"
        log.debug("Injecting %d live headlines (fetched %s)", len(articles), fetched_dt)
        lines = "\n".join(f"• [{a['source']}] {a['title']} ({a['time']})" for a in articles)
        return f"\n\n---\n📰 LIVE HEADLINES (fetched {fetched_dt} IST):\n{lines}\n---"
    except Exception as exc:
        log.warning("Failed to parse headline cache: %s", exc)
        return ""


def build_system(headlines: str, search_results: list[dict], qtype: str, smalltalk: bool = False) -> str:
    from datetime import date
    today = date.today().strftime("%A, %B %d, %Y")

    if smalltalk:
        # Greetings / self-introductions get a short, warm, human reply —
        # no headlines, no web context, no data-dump instructions. This is
        # what keeps "hi" or "hi i am albin" simple instead of triggering a
        # full analyst-style response.
        return f"""You are **Growth Gradual Assistant**, built into the Growth Gradual platform. Today is {today}.

The user just sent a greeting or introduced themselves — nothing else.

**Behaviour:**
- Reply in 1-3 short, warm sentences. No headers, no bullet lists, no tables, no markdown formatting.
- If they gave their name, use it naturally.
- Briefly mention you can help with Indian stocks, mutual funds, IPOs, market news, or general finance questions — in one short sentence, not a list.
- Do NOT pull in market data, headlines, statistics, or citations. Do NOT add a "verify live prices" disclaimer — there's no market data here to verify.
- Keep it conversational, like a person saying hello back — not a report."""

    web_ctx = ""
    if search_results:
        snippets = []
        for i, r in enumerate(search_results[:18]):
            content = r.get("fullContent", "")
            if len(content) > len(r.get("snippet", "")):
                content = content[:1200]
            else:
                content = r.get("snippet", "")
            pub = f" ({r['published']})" if r.get("published") else ""
            snippets.append(f"- {r['title']}\nSource: {r['url']}{pub}\n{content}")
        web_ctx = f"\n\n---\n🌐 WEB SEARCH RESULTS — TOP {len(search_results)} PAGES (with full page content):\n\n" + "\n\n".join(snippets) + "\n---"

    finance_persona = f"""You are **Growth Gradual** — an expert AI assistant for Indian financial markets, built into the Growth Gradual platform. Today is {today}.

You specialise in NSE/BSE stocks, IPOs, mutual funds, RBI/SEBI policy, macroeconomics, and personal finance for Indian investors.

**Voice — sound like a sharp, friendly analyst talking to a client, not a report generator:**
- Write the way a knowledgeable person actually talks — natural sentence rhythm, varied lengths, the occasional "but," "so," or "honestly" where it fits. Avoid stiff openers like "Based on the search results" or "According to the data provided" — just say the thing.
- Lead with the answer, then back it up. Don't pad with throat-clearing before getting to the point.
- Bullets and tables are tools for genuinely list-like or numeric content, not the default shape of every answer — a couple of well-written paragraphs often reads better than a wall of bullet points.
- Have a point of view where the data supports one. Flag genuine uncertainty plainly instead of hedging everything.

**Substance:**
- Give sharp, specific, data-driven answers. When using web results, name the publication inline (e.g. "Moneycontrol reports...", "per the latest RBI bulletin...").
- NEVER use bracket-style citation markers such as [1], [2], or [1, 2] anywhere in your reply — that's a hard rule. Name the source in the sentence itself instead.
- When web results are provided, pull in the real numbers, percentages, dates, and figures from them rather than speaking in generalities.
- Use **bold** for key terms and figures so they're easy to scan.
- Close out market-specific answers with a quick, natural reminder to verify live prices before trading — phrase it like a person would, not a fixed disclaimer line repeated verbatim every time."""

    general_persona = f"""You are **Growth Gradual Assistant** — a knowledgeable AI assistant built into the Growth Gradual platform. Today is {today}.

**Voice — sound like a smart, helpful person, not a search-results summarizer:**
- Write in natural, conversational prose. Skip robotic openers like "Based on the search results" — just answer.
- Use bullets or tables only where the content is genuinely list-like or numeric; otherwise prefer well-organized paragraphs.
- Get to the point quickly, then add the supporting detail and nuance.

**Substance:**
- Answer comprehensively using the provided web search results from the top 18 pages.
- Pull in the real numbers, statistics, dates, and figures found in the sources rather than vague generalities.
- When referencing web content, name the publication inline (e.g. "Reuters notes...") — never use bracket-style citation markers like [1] or [1, 2]. That's a hard rule.
- Use **bold** for key terms where it aids scanning.
- Be thorough, accurate, and helpful. Where relevant, connect the topic back to financial or economic context."""

    base = finance_persona if qtype == "finance" else general_persona
    return base + headlines + web_ctx


# ─── Groq streaming ────────────────────────────────────────────────────────────
_GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # only currently live model on free tier
]

async def stream_groq(system_prompt: str, messages: list[dict]) -> AsyncGenerator[str, None] | None:
    """
    Try every Groq key × model in round-robin order.
    Returns an async generator that yields SSE chunks on success,
    or None if all keys/models are exhausted.
    """
    keys = get_groq_keys()
    if not keys:
        log.warning("Groq: no keys configured")
        return None

    available_keys = [k for k in round_robin(keys) if not is_rate_limited(k)]
    if not available_keys:
        log.warning("Groq: all keys rate-limited — trying anyway with first key")
        available_keys = keys[:1]

    log.info("Groq: attempting with %d key(s) × %d models", len(available_keys), len(_GROQ_MODELS))

    for key in available_keys:
        for model in _GROQ_MODELS:
            combo = f"{key}:{model}"
            if is_rate_limited(combo):
                log.debug("Groq: skipping rate-limited %s ...%s", model, key[-4:])
                continue
            try:
                log.debug("Groq: trying model=%s key=...%s", model, key[-4:])

                # Use a persistent client (not closed with async with) so streaming works
                client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=90, write=30, pool=10))
                req = client.build_request(
                    "POST",
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {key}",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "system", "content": system_prompt}] + messages,
                        "stream": True,
                        "max_tokens": 2048,
                        "temperature": 0.5,
                    },
                )
                res = await client.send(req, stream=True)

                if res.status_code == 429:
                    await res.aclose()
                    await client.aclose()
                    # Parse retry-after header if present
                    retry_after = int(res.headers.get("retry-after", "60"))
                    wait_ms = max(retry_after * 1000, 60_000)
                    log.warning("Groq 429 model=%s key=...%s — RL for %ds", model, key[-4:], retry_after)
                    mark_rate_limited(combo, wait_ms)
                    mark_rate_limited(key, wait_ms)
                    continue

                if res.status_code in (401, 403):
                    await res.aclose(); await client.aclose()
                    log.warning("Groq %d key=...%s — invalid key", res.status_code, key[-4:])
                    mark_rate_limited(key, 24 * 60 * 60_000)
                    break  # try next key, not next model

                if res.status_code == 503:
                    await res.aclose(); await client.aclose()
                    log.warning("Groq 503 model=%s — skipping", model)
                    mark_rate_limited(combo, 30_000)
                    continue

                if not res.is_success:
                    await res.aclose(); await client.aclose()
                    log.warning("Groq HTTP %d model=%s key=...%s", res.status_code, model, key[-4:])
                    continue

                log.info("Groq: streaming started model=%s key=...%s", model, key[-4:])

                async def _stream_and_close(
                    _res: httpx.Response,
                    _client: httpx.AsyncClient,
                    _key: str, _model: str, _combo: str,
                ) -> AsyncGenerator[str, None]:
                    try:
                        async for chunk in _res.aiter_bytes():
                            decoded = chunk.decode(errors="replace")
                            if '"rate_limit_exceeded"' in decoded or '"tokens_exhausted"' in decoded:
                                log.warning("Groq: mid-stream quota hit model=%s", _model)
                                mark_rate_limited(_combo, 60_000)
                                mark_rate_limited(_key, 60_000)
                                return
                            yield decoded
                    finally:
                        await _res.aclose()
                        await _client.aclose()

                return _stream_and_close(res, client, key, model, combo)

            except httpx.ConnectError as exc:
                log.warning("Groq connect error model=%s: %s", model, exc)
                mark_rate_limited(combo, 15_000)
                continue
            except Exception as exc:
                log.warning("Groq exception model=%s key=...%s: %s", model, key[-4:], exc)
                continue

    log.error("Groq: all keys and models exhausted")
    return None


_GEMINI_CHAT_MODELS = [
    # Ordered by free-tier RPD so the highest-quota model is tried first.
    # gemini-3.1-flash-lite: 15 RPM / 500 RPD  ← workhorse
    # gemini-2.5-flash-lite: 10 RPM / 20 RPD
    # gemini-2.5-flash:       5 RPM / 20 RPD
    # gemini-3-flash-preview: 5 RPM / 20 RPD   (API string for "Gemini 3 Flash" in console)
    # gemini-3.5-flash:       5 RPM / 20 RPD
    # Removed: gemini-1.5-flash / gemini-1.5-flash-8b (404, retired)
    #          gemini-2.0-flash / gemini-2.0-flash-lite (retiring June 2026, 0/0/0 quota)
    "gemini-3.1-flash-lite",   # 15 RPM / 500 RPD
    "gemini-2.5-flash-lite",   # 10 RPM / 20 RPD
    "gemini-2.5-flash",        #  5 RPM / 20 RPD
    "gemini-3-flash-preview",  #  5 RPM / 20 RPD
    "gemini-3.5-flash",        #  5 RPM / 20 RPD
]


async def gemini_sse(system_prompt: str, messages: list[dict]) -> AsyncGenerator[str, None]:
    keys = get_gemini_keys()
    if not keys:
        raise ValueError("No Gemini keys configured")

    user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    available_keys = [k for k in round_robin(keys) if not is_rate_limited(k)]
    if not available_keys:
        log.warning("Gemini: all keys rate-limited — trying first key anyway")
        available_keys = keys[:1]

    # Try models first, then rotate keys — maximises chance of hitting a non-RL slot
    attempts = [(k, m) for m in _GEMINI_CHAT_MODELS for k in available_keys]
    log.info("Gemini chat: %d key(s) × %d models = %d attempts", len(available_keys), len(_GEMINI_CHAT_MODELS), len(attempts))

    for key, model in attempts:
        combo = f"{key}:{model}"
        if is_rate_limited(combo):
            continue
        try:
            log.debug("Gemini chat: model=%s key=...%s", model, key[-4:])
            async with httpx.AsyncClient(timeout=45) as client:
                res = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    json={
                        "system_instruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.5},
                    },
                )

            if res.status_code == 429:
                retry_after = int(res.headers.get("retry-after", "60"))
                wait_ms = max(retry_after * 1000, 60_000)
                log.warning("Gemini 429 model=%s key=...%s — RL for %ds", model, key[-4:], retry_after)
                mark_rate_limited(combo, wait_ms)
                mark_rate_limited(key, wait_ms)
                continue
            if res.status_code == 503:
                log.warning("Gemini 503 model=%s key=...%s — RL for 30s", model, key[-4:])
                mark_rate_limited(combo, 30_000)
                continue
            if res.status_code in (401, 403):
                log.warning("Gemini %d key=...%s — invalid key", res.status_code, key[-4:])
                mark_rate_limited(key, 24 * 60 * 60_000)
                break  # try next key entirely
            if not res.is_success:
                log.warning("Gemini HTTP %d model=%s key=...%s", res.status_code, model, key[-4:])
                continue

            data = res.json()
            # Check for quota errors in response body
            if "error" in data:
                err_status = data["error"].get("code", 0)
                err_msg    = data["error"].get("message", "")
                if err_status == 429 or "quota" in err_msg.lower() or "rate" in err_msg.lower():
                    log.warning("Gemini quota error model=%s: %s", model, err_msg[:80])
                    mark_rate_limited(combo, 60_000)
                    mark_rate_limited(key, 60_000)
                    continue

            text = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if not text:
                log.warning("Gemini: empty response model=%s key=...%s", model, key[-4:])
                continue

            log.info("Gemini chat: got %d chars model=%s key=...%s", len(text), model, key[-4:])
            # Stream word-by-word for a natural feel
            words = text.split(" ")
            for i, word in enumerate(words):
                suffix = " " if i < len(words) - 1 else ""
                yield f'data: {json.dumps({"choices": [{"delta": {"content": word + suffix}}]})}\n\n'
            yield "data: [DONE]\n\n"
            return

        except Exception as exc:
            log.warning("Gemini exception model=%s key=...%s: %s", model, key[-4:], exc)
            continue

    raise RuntimeError("All Gemini key×model combinations failed or rate-limited")




# ─── Citation-marker stripping (streaming-safe) ────────────────────────────────
# The system prompts tell the model never to use bracket citation numbers like
# [1] or [1, 2] — but models occasionally slip one in anyway. This filter is a
# belt-and-suspenders backstop that removes them from the live token stream,
# the same way report.py strips them from generated reports. It buffers a
# small tail of text so a marker split across two network chunks (e.g. one
# chunk ending in "[" and the next starting with "1]") still gets caught.
_CITATION_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]")


class _CitationStripper:
    _MAX_HOLD = 24  # generous upper bound for something like "[12, 34, 56]"

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        out = []
        i, n = 0, len(self._buf)
        while i < n:
            if self._buf[i] == "[":
                m = _CITATION_RE.match(self._buf, i)
                if m:
                    i = m.end()
                    continue
                rest = self._buf[i:]
                if len(rest) <= self._MAX_HOLD and re.match(r"^\[[\d,\s]*$", rest):
                    # Could still become a citation marker once more text
                    # arrives — hold this tail back and wait for the next chunk.
                    break
            out.append(self._buf[i])
            i += 1
        self._buf = self._buf[i:]
        return "".join(out)

    def flush(self) -> str:
        rest, self._buf = self._buf, ""
        return rest


async def _sanitize_and_forward(raw_gen, assistant_chunks: list[str]) -> AsyncGenerator[bytes, None]:
    """
    Wrap an upstream OpenAI-style SSE generator (Groq's raw passthrough or
    Gemini's word-by-word stream), strip bracket-citation markers out of the
    text as it streams, and yield clean `data: {...}\\n\\n` bytes shaped the
    way the frontend expects. Also appends the cleaned text to
    `assistant_chunks` so the saved transcript matches exactly what the user saw.
    """
    stripper = _CitationStripper()
    leftover = ""
    async for chunk in raw_gen:
        raw = chunk if isinstance(chunk, str) else chunk.decode(errors="replace")
        leftover += raw
        lines = leftover.split("\n")
        leftover = lines.pop()  # keep a possibly-incomplete trailing line for next round
        for line in lines:
            line = line.rstrip("\r")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("[DONE]", ""):
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("type") == "meta":
                yield f"data: {payload}\n\n".encode()
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if not content:
                continue
            cleaned = stripper.feed(content)
            if cleaned:
                assistant_chunks.append(cleaned)
                yield f"data: {json.dumps({'choices': [{'delta': {'content': cleaned}}]})}\n\n".encode()
    tail = stripper.flush()
    if tail:
        assistant_chunks.append(tail)
        yield f"data: {json.dumps({'choices': [{'delta': {'content': tail}}]})}\n\n".encode()


# ─── Main handler ──────────────────────────────────────────────────────────────
@router.post("")
async def chat(request: Request):
    t0 = time.perf_counter()
    try:
        body = await request.json()
    except Exception:
        body = {}

    messages: list[dict] = body.get("messages", [])
    file_context: str = body.get("fileContext", "")
    file_images: list[dict] = body.get("fileImages", [])
    has_rag:      bool = bool(body.get("hasRag", False))
    # sessionId is optional — only persists when provided
    session_id: str = (body.get("sessionId") or "").strip()

    if not messages:
        return JSONResponse({"error": "No messages"}, status_code=400)

    last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    do_search = needs_web_search(last_user_msg, has_files=bool(file_context or file_images))
    qtype = classify_query(last_user_msg)
    # Only treat as smalltalk when there's no file/RAG context riding along —
    # a greeting attached to an uploaded file still needs the normal flow.
    is_smalltalk_msg = (
        is_smalltalk(last_user_msg)
        and not file_context and not file_images and not has_rag
    )

    # ── Query rewrite: expand pronoun-heavy follow-ups into self-contained queries ──
    # Run only when search or RAG will actually use the query (saves a Groq call otherwise)
    search_query = last_user_msg
    if do_search or has_rag:
        # Pass history excluding the current (last) user message so context is prior turns
        prior_history = messages[:-1] if messages and messages[-1].get("role") == "user" else messages
        search_query = await rewrite_query_for_search(last_user_msg, prior_history)

    log.info(
        "Chat request: msg=%r  search_query=%r  search=%s  type=%s  file_ctx=%s  rag=%s  smalltalk=%s  session=%s",
        last_user_msg[:80], search_query[:80] if search_query != last_user_msg else "(unchanged)",
        do_search, qtype, bool(file_context), has_rag, is_smalltalk_msg, session_id or "none",
    )

    # Upsert the session row upfront (fire-and-forget — don't block the stream)
    if session_id:
        asyncio.ensure_future(_upsert_session(session_id))

    async def _no_search() -> list:
        return []

    async def _no_headlines() -> str:
        return ""

    search_results, headlines = await asyncio.gather(
        tavily_search(search_query, max_results=20, min_results=10) if do_search else _no_search(),
        load_headlines(30) if not is_smalltalk_msg else _no_headlines(),
    )

    base_prompt = build_system(headlines, search_results, qtype, smalltalk=is_smalltalk_msg)

    # ── Image vision: extract content from attached images via Gemini Vision ─
    # ── Image vision: check cache first, then Gemini Vision for new images ──
    if file_images:
        try:
            from routes.report import extract_data_from_images

            # Check Supabase cache first — avoid re-running Vision on same image
            img_hashes = [_image_hash(img["data"]) for img in file_images]
            cached = await _load_cached_extractions(session_id, img_hashes) if session_id else {}

            cached_texts  = [cached[h] for h in img_hashes if h in cached]
            uncached_imgs = [img for img, h in zip(file_images, img_hashes) if h not in cached]

            image_context_parts = cached_texts[:]

            if uncached_imgs:
                fresh_text = await extract_data_from_images(last_user_msg, uncached_imgs)
                if fresh_text:
                    image_context_parts.append(fresh_text)
                    # Map each uncached image hash → the fresh extraction text,
                    # then persist to Supabase in the background
                    fresh_extractions = {_image_hash(img["data"]): fresh_text for img in uncached_imgs}
                    asyncio.ensure_future(
                        _store_image_extractions(session_id, uncached_imgs, fresh_extractions)
                    )

            image_context = "\n\n".join(image_context_parts)
            if image_context:
                src = ("cache+vision" if cached_texts and uncached_imgs
                       else "cache" if cached_texts else "vision")
                file_context += f"\n\n━━ IMAGE CONTENT ({src}) ━━\n{image_context}\n━━ END ━━"
                log.info("Chat: image context %s — %d chars, %d image(s), %d cached",
                         src, len(image_context), len(file_images), len(cached_texts))
        except Exception as exc:
            log.warning("Chat: image extraction failed: %s", exc)

    # ── RAG grounding: use RAG service system prompt when files are indexed ───
    rag_system_prompt = ""
    if has_rag and session_id:
        log.info("Chat: RAG mode — querying for session %s question=%r  search_query=%r",
                 session_id[:8], last_user_msg[:60], search_query[:60])
        rag_result = await _rag_query(
            session_id=session_id,
            question=search_query,
            top_k=8,
            min_score=0.15,
        )
        if rag_result.get("has_content") and rag_result.get("system_prompt"):
            rag_system_prompt = rag_result["system_prompt"]
            log.info("Chat: RAG grounded — %d chunks from %s",
                     rag_result.get("retrieved", 0), rag_result.get("source_files", []))
        else:
            log.warning("Chat: RAG returned no content — chunks=%s has_content=%s — falling back to LLM",
                        rag_result.get("retrieved", 0), rag_result.get("has_content"))

    if rag_system_prompt:
        # RAG has content — use it as the system prompt but append web search if available
        if search_results:
            rag_system_prompt += f"\n\n## SUPPLEMENTARY WEB SEARCH RESULTS\nThe following live web results may supplement the document content:\n{base_prompt}"
        system_prompt = rag_system_prompt
    else:
        # No RAG content — use full web search prompt as normal
        system_prompt = base_prompt + file_context if file_context else base_prompt

    meta_event = (
        "data: "
        + json.dumps({
            "type": "meta",
            "searchPerformed": do_search,
            "resultCount": len(search_results),
            "queryType": qtype,
            "sources": [
                {"title": r["title"], "url": r["url"], "snippet": r["snippet"][:180]}
                for r in search_results
            ],
        })
        + "\n\n"
    )

    async def generate() -> AsyncGenerator[bytes, None]:
        assistant_chunks: list[str] = []
        provider_used = "unknown"

        yield meta_event.encode()

        # ── Try Groq (all keys × models, round-robin) ─────────────────────────
        groq_gen = await stream_groq(system_prompt, messages)
        if groq_gen is not None:
            provider_used = "groq"
            try:
                async for out_bytes in _sanitize_and_forward(groq_gen, assistant_chunks):
                    yield out_bytes
                yield b"data: [DONE]\n\n"
                elapsed = int((time.perf_counter() - t0) * 1000)
                log.info("Chat complete via Groq in %dms", elapsed)
                if session_id:
                    full_reply = "".join(assistant_chunks)
                    asyncio.ensure_future(
                        _save_messages(session_id, last_user_msg, full_reply, provider_used, elapsed)
                    )
                return
            except Exception as exc:
                log.warning("Groq stream broke mid-way: %s — falling back to Gemini", exc)

        # ── Gemini fallback (all keys × models, round-robin) ──────────────────
        gemini_keys = get_gemini_keys()
        if gemini_keys:
            provider_used = "gemini"
            try:
                async for out_bytes in _sanitize_and_forward(gemini_sse(system_prompt, messages), assistant_chunks):
                    yield out_bytes
                yield b"data: [DONE]\n\n"
                elapsed = int((time.perf_counter() - t0) * 1000)
                log.info("Chat complete via Gemini in %dms", elapsed)
                if session_id:
                    full_reply = "".join(assistant_chunks)
                    asyncio.ensure_future(
                        _save_messages(session_id, last_user_msg, full_reply, provider_used, elapsed)
                    )
                return
            except Exception as e:
                log.error("All LLM providers failed: %s", e)

        # ── All providers exhausted — tell user clearly ────────────────────────
        log.error("Chat: all providers rate-limited or failed")
        err_msg = (
            "⚠️ All AI providers are currently rate-limited. "
            "This is a temporary limit on the free tier — please wait 60 seconds and try again. "
            "If this keeps happening, additional API keys can be added to GROQ_API_KEYS / GEMINI_API_KEY in your environment."
        )
        err_chunk = json.dumps({"choices": [{"delta": {"content": err_msg}}]})
        yield f"data: {err_chunk}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
