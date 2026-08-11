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
from urllib.parse import urlparse
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
from datetime import timezone as _tz
from email.utils import parsedate_to_datetime as _parse_rfc2822
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

# ─── Domain filtering ────────────────────────────────────────────────────────
# Previously this searched only inside a fixed, hand-curated list of ~60
# "approved" finance domains via Tavily's include_domains. That meant results
# were only ever as good as the list — anything genuinely relevant that
# wasn't on it got excluded, and (per production logs) include_domains isn't
# reliably enforced by Tavily when topic="finance" is also set, so off-list
# junk (dictionary sites, YouTube, Facebook) leaked in anyway and got
# mislabeled "trusted" since the code assumed the restriction had worked.
#
# Sources are now chosen dynamically per query — Tavily's own relevance
# ranking (plus topic="finance"/country="india" biasing) decides what's
# actually relevant to what was asked, instead of a static allow-list.
#
# The one thing still filtered out is a small denylist of domains that are
# never a legitimate source for *any* query here — dictionaries, video/
# social platforms, forums. This is a category exclusion, not a finance
# allow-list: it doesn't grant trust to any domain, it just removes the
# specific kinds of noise that caused the original bug.
_JUNK_DOMAIN_SUFFIXES = [
    # dictionaries / language-learning — never a market/finance source
    "merriam-webster.com", "dictionary.com", "cambridge.org",
    "oxfordlearnersdictionaries.com", "langeek.co", "vocabulary.com",
    "thesaurus.com", "wiktionary.org",
    # video / social — never a citable primary source for a research report
    "youtube.com", "youtu.be", "facebook.com", "instagram.com",
    "tiktok.com", "x.com", "twitter.com", "reddit.com", "pinterest.com",
    "quora.com",
]


def _is_junk_domain(url: str) -> bool:
    """True if url's host falls in the never-a-source denylist above."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _JUNK_DOMAIN_SUFFIXES)

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

# Terms that, on their own, unambiguously mean "Indian stock/investing markets" —
# no other business/agency/personal-finance context realistically uses them.
# Classification hinges on these; the generic FINANCE_TERMS list above is too
# broad on its own (see STRONG_FINANCE_TERMS note below).
STRONG_FINANCE_TERMS = [
    "nifty", "sensex", "bse", "nse", "ipo", "rbi", "sebi", "amfi",
    "nifty 50", "nifty bank", "nifty it", "nifty auto", "nifty fmcg",
    "stock", "share", "equity", "mutual fund", "nav", "sip", "dividend",
    "demat", "etf", "reit", "aif", "pms", "fii", "dii", "gmp",
    "ipo allotment", "grey market", "buyback", "rights issue", "qip", "ofs",
    "block deal", "bulk deal", "bond", "gilt", "treasury", "g-sec",
    "repo rate", "monetary policy", "rupee depreciation", "trade deficit",
    "lic", "hdfc", "icici", "sbi", "axis", "kotak", "reliance", "tata",
    "infosys", "wipro", "tcs", "adani", "bajaj", "zerodha", "groww",
    "paytm", "zomato", "ola", "swiggy", "nykaa", "delhivery",
    "penny stock", "mid cap", "small cap", "large cap", "bluechip",
    "52 week", "all time high", "ath", "f&o", "fno", "derivative",
    "futures", "options", "expiry", "bearish", "bullish", "breakout",
    "support", "resistance", "roe", "roce", "npa", "credit growth",
]

# Words that strongly signal "this is about running MY business/agency/service,
# not the stock market" — even though they overlap vocabulary-wise with generic
# finance words like "revenue," "profit," or "market." If present, treat the
# query as general regardless of how many weak FINANCE_TERMS matched, since
# these are decisive context clues a stock-market persona has no business
# answering (e.g. hiring plans, client counts, service pricing).
BUSINESS_OPS_OVERRIDE_TERMS = [
    "client", "clients", "customer", "customers", "employee", "employees",
    "hire", "hiring", "hired", "staff", "team of", "my team", "my agency",
    "my business", "my company", "my startup", "startup idea", "business idea",
    "freelance", "freelancer", "agency", "digital marketing", "app development",
    "website development", "web development", "saas", "retainer", "clientele",
    "service business", "consulting business", "my clients",
]


def classify_query(msg: str) -> str:
    m = msg.lower()
    if any(t in m for t in BUSINESS_OPS_OVERRIDE_TERMS):
        return "general"
    if any(t in m for t in STRONG_FINANCE_TERMS):
        return "finance"
    # No unambiguous market term present — fall back to the broad list, but
    # require at least 2 distinct hits so a single generic word like "profit"
    # or "current" (common in any business/general question) doesn't alone
    # misroute the query into the stock-market persona.
    weak_hits = {t for t in FINANCE_TERMS if t in m}
    return "finance" if len(weak_hits) >= 2 else "general"

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

# ─── Deterministic Tavily query optimizer (no LLM call) ────────────────────
# Converts a verbose prompt into one or more concise, Google-style search
# queries. Only genuinely ambiguous conversational follow-ups still use the
# Groq rewrite further below — everything else goes through here.

_FILLER_PHRASES = [
    # multi-word phrases listed first so they're stripped whole
    "support with data",
    "easy to understand",
    "detailed analysis",
    "in detail",
    "explain",
    "analyze",
    "analyse",
    "compare",
    "detailed",
    "report",
    "reports",
    "charts",
    "chart",
    "uhni",
    "reasoning",
]
_FILLER_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _FILLER_PHRASES) + r")\b",
    re.IGNORECASE,
)

# Split "A and B" into two independent queries by default — EXCEPT when the
# phrase ends in a word that ties both halves into one combined ask (a
# comparison, or a shared trailing noun that describes both entities
# together). That's the signal that distinguishes:
#   "Promoter exits and sector rotation"      -> 2 independent topics, split
#   "FTAs and exports"                        -> 2 independent topics, split
#   "Reliance and Tata Motors comparison"     -> 1 joint comparison, no split
#   "Infosys and TCS financial results"       -> 1 joint ask, no split
#   "Nifty IT and Banking sectors"            -> 1 joint ask, no split
_MULTI_INTENT_SPLIT_RE = re.compile(r"^(.*?)\s+and\s+(.*)$", re.IGNORECASE)

_JOINT_SUFFIX_RE = re.compile(
    r"\b(comparison|compared to|vs\.?|versus|financial results|results|"
    r"performance|earnings|outlook|sector|sectors|stocks|shares|"
    r"valuation|review)\s*$",
    re.IGNORECASE,
)

# Only these words justify adding "latest" — don't add it speculatively.
_EXPLICIT_RECENCY_RE = re.compile(
    r"\b(latest|recent|recently|current|currently|now|today)\b", re.IGNORECASE,
)
_HAS_LATEST_RE = re.compile(r"\blatest\b", re.IGNORECASE)
_THIS_YEAR_RE = re.compile(r"\b(this year|current year)\b", re.IGNORECASE)
_THIS_WEEK_RE = re.compile(r"\b(this week|past week|last week)\b", re.IGNORECASE)
_THIS_MONTH_RE = re.compile(r"\b(this month|past month|last month)\b", re.IGNORECASE)

# Same signal as report.py's _HISTORICAL_INTENT_RE, duplicated locally to
# avoid a circular import (report.py already imports from chat.py).
_QUERY_HISTORICAL_RE = re.compile(
    r"\b(quarter|qtr|q[1-4]\b|quarterly|past \d+ (?:quarters?|months?|years?)|"
    r"since (?:19|20)\d{2}|yoy|qoq|year[- ]on[- ]year|year[- ]over[- ]year|"
    r"quarter[- ]on[- ]quarter|quarter[- ]over[- ]quarter|trend|historical|history|"
    r"over time|comparison|compare[ds]?|"
    r"last \d+ (?:quarters?|months?|years?))\b",
    re.IGNORECASE,
)

_MAX_QUERY_LEN = 120
_HISTORICAL_SUFFIX = " quarter-wise comparison historical data"


def _clean_query_text(text: str) -> str:
    """Strip filler words/phrases and collapse leftover whitespace/punctuation."""
    cleaned = _FILLER_RE.sub(" ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .,:;-")
    return cleaned


def _finalize_query(text: str, current_year: int | None = None) -> str:
    """Apply latest/year/historical-suffix rules and enforce the length budget."""
    cleaned = _clean_query_text(text) or text.strip()

    if _THIS_YEAR_RE.search(text):
        year = current_year or _dt.now().year
        cleaned = (
            _THIS_YEAR_RE.sub(str(year), cleaned)
            if _THIS_YEAR_RE.search(cleaned) else f"{cleaned} {year}"
        )

    if _EXPLICIT_RECENCY_RE.search(text) and not _HAS_LATEST_RE.search(cleaned):
        cleaned = f"{cleaned} latest"

    if _QUERY_HISTORICAL_RE.search(text) and "historical" not in cleaned.lower():
        cleaned = f"{cleaned}{_HISTORICAL_SUFFIX}"

    if len(cleaned) > _MAX_QUERY_LEN:
        cleaned = cleaned[:_MAX_QUERY_LEN].rsplit(" ", 1)[0]

    return cleaned


def optimize_search_query(text: str, current_year: int | None = None, qtype: str = "general") -> list[str]:
    """
    Deterministic, LLM-free conversion of a user prompt into one or more
    concise Tavily-style search queries.

    - Strips instructional filler (explain, analyze, compare, detailed,
      report, support with data, charts, UHNI, reasoning, easy to
      understand). Entities (companies, sectors, countries, years, quarters,
      financial terms) are untouched since none overlap the filler list.
    - Adds "latest" only when the ORIGINAL text explicitly said
      latest/recent/current/now/today.
    - Replaces "this year"/"current year" with the actual year.
    - Appends a short historical-comparison hint when quarter/YoY/"past N
      quarters"-style language is present.
    - Targets 50-120 chars; truncates on a word boundary if it runs long
      (never pads a short query just to hit a floor).
    - Splits "A and B" into two queries UNLESS the phrase ends in a shared
      trailing word/phrase (comparison, results, sectors, vs, etc.) that
      ties both halves into one combined ask — that's the difference
      between "Promoter exits and sector rotation" (2 independent topics)
      and "Reliance and Tata Motors comparison" (1 joint comparison).
      Splitting is only attempted for finance queries (qtype="finance") —
      its examples and design (comparing named entities/sectors) don't
      generalize to a general-purpose narrative message. A general query
      describing one continuous situation ("I'm at 1L/month and I hired
      two people and my profit is...") isn't two search intents just
      because it contains the word "and"; splitting it at the first "and"
      produces one truncated fragment and one garbled grab-bag of the
      remaining unrelated clauses, neither of which is a usable search
      query. See the finance-only guard below.

    NOTE: being rule-based, this won't paraphrase/reorder as fluently as an
    LLM would (e.g. it won't expand "FTAs" to "free trade agreements" or
    reorder "List FTAs signed by India" into "India free trade agreements
    signed") — it only removes filler and applies the rules above. That
    trade-off is the point: it's free and instant instead of a Groq call.
    """
    text = text.strip()
    if not text:
        return [text]

    stripped_end = text.rstrip(" ?.!")
    if qtype == "finance":
        m = _MULTI_INTENT_SPLIT_RE.match(text)
        if m and not _JOINT_SUFFIX_RE.search(stripped_end):
            first, second = m.group(1).strip(), m.group(2).strip()
            if first and second:
                return [
                    _finalize_query(first, current_year),
                    _finalize_query(second, current_year),
                ]

    return [_finalize_query(text, current_year)]


# Only these short, context-dependent follow-ups justify the extra Groq
# call — a normal prompt (however long) should never match this.
_AMBIGUOUS_FOLLOWUP_PHRASES_RE = re.compile(
    r"^\s*(what about|how about|what changed|what's changed|any update|"
    r"what next|what happened)\b",
    re.IGNORECASE,
)


def _is_ambiguous_followup(msg: str) -> bool:
    """
    True only for short, context-dependent follow-ups such as "What about
    Infosys?", "How about its competitors?", "What changed?" — these can't
    be turned into a standalone query by rule-based cleanup because the
    actual subject lives in the prior turn, not in this message.
    """
    if len(msg) > 100 or len(msg.split()) > 10:
        return False
    return bool(_AMBIGUOUS_FOLLOWUP_PHRASES_RE.match(msg) or _FOLLOWUP_RE.search(msg))


# ─── Freshness: sort newer sources first ────────────────────────────────────
# Recency filtering now happens at search time via Tavily's `time_range`
# param (see _detect_recency_time_range below), so results are no longer
# discarded here just for being older than a fixed cutoff — that risked
# dropping perfectly relevant results whenever the query's recency intent
# wasn't caught by the pre-filter, or whenever a source's date was simply
# stale-parsed. Post-processing is now sort-only.


def _parse_published_date(value: str | None):
    """Best-effort parse of Tavily's `published_date` field. Never raises —
    returns None for missing/unrecognized formats, which callers treat as
    'unknown age', not 'old'."""
    if not value:
        return None
    for parser in (_parse_rfc2822, _dt.fromisoformat):
        try:
            dt = parser(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt
        except Exception:
            continue
    return None


def _sort_and_filter_by_freshness(results: list[dict], keep_old: bool = True) -> list[dict]:
    """
    Sort results newest-first by published date. Results are never dropped
    for being old — actual recency filtering happens upstream via Tavily's
    `time_range` param (see _detect_recency_time_range), which is a more
    precise signal than an arbitrary fixed-age cutoff applied after the
    fact. Results with no parseable date are kept (unknown age isn't
    treated as "old") and placed after the dated ones, in their original
    order.

    `keep_old` is accepted for call-site compatibility (existing callers
    pass historical_intent here) but no longer changes behaviour — it's
    kept purely so callers don't need updating.
    """
    dated: list[tuple] = []
    undated: list[dict] = []
    for r in results:
        published_at = _parse_published_date(r.get("published"))
        if published_at is None:
            undated.append(r)
            continue
        dated.append((published_at, r))
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [r for _, r in dated] + undated


async def rewrite_query_for_search(last_msg: str, history: list[dict], qtype: str = "general") -> list[str]:
    """
    Turns `last_msg` into one or more Tavily-ready search queries.

    - Genuinely ambiguous conversational follow-ups ("What about Infosys?",
      "How about its competitors?", "What changed?") get a single
      lightweight Groq call, grounded in the last 4 turns of history — the
      ONLY case that costs an extra LLM call.
    - Everything else (a normal prompt, however long) is handled
      deterministically by optimize_search_query() — no LLM call, and no
      change in per-request latency/cost from before.

    Falls back to optimize_search_query(last_msg, qtype=qtype) on any error.
    """
    if not _is_ambiguous_followup(last_msg):
        return optimize_search_query(last_msg, qtype=qtype)

    # Build a compact context from the last 4 turns (2 user + 2 assistant)
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-4:]
    if not recent:
        return optimize_search_query(last_msg, qtype=qtype)

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
        return optimize_search_query(last_msg, qtype=qtype)

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
                log.info("Query rewrite (LLM, ambiguous follow-up): %r → %r", last_msg[:60], rewritten[:80])
                return [rewritten]
    except Exception as exc:
        log.debug("Query rewrite failed (non-critical): %s", exc)

    return optimize_search_query(last_msg, qtype=qtype)


# ─── Country detection for Tavily's `country` boost param ──────────────────
# Tavily docs: country boosts ranking toward that country's sources, but is
# "available only if topic is general" — it's a no-op when topic="finance"
# (which is what most queries here use). We still set it on every call so it
# takes effect for non-finance queries, and so geographic bias is correct if
# Tavily lifts that restriction later. For finance-topic queries, geographic
# relevance still comes from Tavily's own query-driven ranking.
GLOBAL_TERMS = [
    "globally", "global", "worldwide", "world", "international",
    "around the world", "across the world", "all countries", "every country",
]

# country name -> Tavily country value. Add more as needed; keep keys lowercase.
COUNTRY_ALIASES: dict[str, list[str]] = {
    "india":           ["india", "indian", "bharat", "rupee", "inr", " rbi", "sebi", "nse", "bse", "nifty", "sensex"],
    "united kingdom":  ["uk", "u.k.", "united kingdom", "britain", "british", "england", "scotland",
                         "wales", "pound sterling", "gbp", "ftse", "lse", "bank of england"],
    "united states":   ["usa", "u.s.", "u.s.a.", "united states", "america", "american",
                         "nasdaq", "dow jones", "s&p 500", "wall street", "federal reserve", " fed ", "dollar"],
    "canada":          ["canada", "canadian", "tsx"],
    "australia":       ["australia", "australian", "asx"],
    "singapore":       ["singapore", "sgx"],
    "japan":           ["japan", "japanese", "nikkei", "yen"],
    "china":           ["china", "chinese", "yuan", "shanghai composite", "shenzhen"],
    "germany":         ["germany", "german", "dax"],
    "france":          ["france", "french", "cac 40"],
    "united arab emirates": ["uae", "dubai", "abu dhabi", "emirates"],
}


def detect_country(query: str) -> str | None:
    """
    "" / no mention             -> "india" (default)
    explicit country mentioned  -> that country's Tavily value
    "global"/"world"/"worldwide"-> None (no country bias — search everything)
    """
    q = f" {query.lower()} "
    if any(term in q for term in GLOBAL_TERMS):
        return None
    for country, keywords in COUNTRY_ALIASES.items():
        if any(kw in q for kw in keywords):
            return country
    return "india"


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


# ─── Intent-aware time_range selection ──────────────────────────────────────
# Maps recency language straight to Tavily's own `time_range` param
# ("day"/"week"/"month"/"year") so Tavily does the recency filtering at
# search time, instead of unrestricted results being fetched and then
# discarded by age in post-processing. Reuses the same regex constants as
# the query-rewriting/historical-suffix logic above (_EXPLICIT_RECENCY_RE,
# _THIS_YEAR_RE, _QUERY_HISTORICAL_RE) so "recent"/"historical" mean the
# same thing everywhere in this file.
#
# Rules, checked in this order (most specific / highest-priority first):
#   1. Historical / time-series language (quarters, "since 2019", "trend",
#      "YoY", "QoQ", "comparison", "over time", "last N quarters", ...)
#      -> None. These queries want the full history, so time_range is left
#      unset and Tavily returns its normal, unrestricted relevance ranking.
#      This always wins even if a recency word also appears in the same
#      query (e.g. "current trend" is still historical/unrestricted).
#   2. this week / past week / last week          -> "week"
#   3. this month / past month / last month        -> "month"
#   4. this year / current year                    -> "year"
#   5. latest / recent / current / today (and the
#      close synonyms "recently"/"currently"/"now") -> "day"
def _detect_recency_time_range(query: str) -> str | None:
    """
    Return the Tavily `time_range` value that matches the query's recency
    intent, or None if there isn't one / the query is historical in nature.
    Only post-processing left downstream is sorting by publication date —
    this is what actually restricts *which* results Tavily returns.
    """
    if _QUERY_HISTORICAL_RE.search(query):
        return None
    if _THIS_WEEK_RE.search(query):
        return "week"
    if _THIS_MONTH_RE.search(query):
        return "month"
    if _THIS_YEAR_RE.search(query):
        return "year"
    if _EXPLICIT_RECENCY_RE.search(query):
        return "day"
    return None


async def _tavily_one_call(
    key: str, query: str, max_results: int, qtype: str,
    country: str | None = "india", images_out: list | None = None,
    time_range: str | None = None,
) -> list[dict]:
    """
    One Tavily SDK call on a single key. No include_domains restriction —
    which domains actually show up is decided per-query by Tavily's own
    relevance ranking (biased by topic="finance"/country when applicable),
    not by a fixed allow-list. country=None means no geographic boost (used
    for "globally"/"world" style queries). Returns [] on any failure —
    caller decides what to do with a thin/empty result.

    images_out: if provided, any images Tavily returns for this query are
    appended to it in-place as {"url": ..., "description": ...} dicts. Safe
    to share one list across several concurrent calls (asyncio is single-
    threaded, so list.append from each coroutine can't interleave/corrupt).

    time_range: Tavily's freshness filter ("day"/"week"/"month"/"year"). When
    set, search results are biased toward sources published within that
    window instead of competing purely on relevance score. Pass None for no
    freshness bias (the previous, default behaviour).
    """
    try:
        from tavily import AsyncTavilyClient
    except ImportError:
        return await _tavily_search_httpx_fallback(query, max_results, qtype, images_out, time_range)

    if is_rate_limited(key):
        return []

    try:
        client = AsyncTavilyClient(api_key=key)
        search_kwargs: dict = {
            "query": query,
            "search_depth": "advanced",
            "max_results": max_results,
            "chunks_per_source": 3,
            "include_answer": "advanced",
            "include_raw_content": True,
            "include_images": images_out is not None,
            "include_image_descriptions": images_out is not None,
            "include_favicon": False,
        }
        if country:
            search_kwargs["country"] = country
        if qtype == "finance":
            search_kwargs["topic"] = "finance"
        if time_range:
            search_kwargs["time_range"] = time_range

        data = await client.search(**search_kwargs)
        raw_results = data.get("results") or []

        before = len(raw_results)
        raw_results = [r for r in raw_results if not _is_junk_domain(r.get("url", ""))]
        dropped = before - len(raw_results)
        if dropped:
            log.info(
                "Tavily key ...%s: dropped %d junk-domain result(s) (dictionary/social/video) "
                "for query %r", key[-4:], dropped, query[:60],
            )

        if images_out is not None:
            for img in (data.get("images") or []):
                if isinstance(img, dict):
                    url, desc = img.get("url", ""), img.get("description", "")
                else:
                    url, desc = str(img), ""
                if url:
                    images_out.append({"url": url, "description": desc})

        results: list[dict] = []
        for r in raw_results:
            chunks = r.get("chunks") or []
            chunk_text = "\n\n".join(c.get("content", "") for c in chunks if c.get("content"))
            raw_content = chunk_text or r.get("raw_content") or r.get("content") or ""
            results.append({
                "title":       r.get("title", ""),
                "url":         r.get("url", ""),
                "snippet":     r.get("content", "")[:800],
                "fullContent": raw_content[:5000],
                "score":       r.get("score"),
                "published":   r.get("published_date"),
            })
        return [_clean_result_content(r) for r in results]

    except Exception as exc:
        err_str = str(exc).lower()
        if "429" in err_str or "rate" in err_str:
            log.warning("Tavily 429/rate-limit on key ...%s — backing off 60s", key[-4:])
            mark_rate_limited(key, 60_000)
        elif "401" in err_str or "403" in err_str or "invalid" in err_str:
            log.warning("Tavily auth error on key ...%s — banning 24h", key[-4:])
            mark_rate_limited(key, 24 * 60 * 60_000)
        else:
            log.warning("Tavily exception key ...%s: %s", key[-4:], exc)
        return []


async def _enrich_thin_results(results: list[dict]) -> list[dict]:
    """Fetch full page content for results whose fullContent is still thin."""
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
    return results


async def tavily_search(
    query: str, max_results: int = 20, min_results: int = 10, images_out: list | None = None,
    historical_intent: bool = False,
) -> list[dict]:
    """
    NOTE: min_results is accepted for call-site compatibility but no longer
    changes behaviour — with the domain allow-list removed there's no
    "trusted vs. general" tier to top up, so every configured key is always
    queried. Kept as a parameter so existing callers don't need updating.

    Multi-key fan-out search — every available Tavily key is queried in
    parallel with the *same* query (no per-key domain restriction), results
    are deduped by URL, and each result that isn't in the junk-domain
    denylist (see _JUNK_DOMAIN_SUFFIXES) is kept and tagged
    "trusted_source": True. What comes back is entirely a function of the
    query itself — Tavily's relevance ranking (plus topic="finance" and
    country biasing) decides which sites are relevant, rather than a fixed
    curated list deciding it up front.

    Falls back to the old single-key round-robin behaviour if only one key
    is configured, or continues to the httpx fallback if the SDK path fails
    entirely.

    images_out: if provided, images Tavily found for this query (deduped by
    the caller) are appended to it as {"url", "description"} dicts. Only the
    callers that actually want images (report generation) need to pass this —
    everyone else gets the exact same behaviour as before.

    historical_intent: accepted for call-site compatibility. Results are no
    longer dropped by age here — recency is now enforced upstream via
    Tavily's `time_range` param (see _detect_recency_time_range) rather than
    a fixed-age post-filter, so this flag no longer changes what comes back;
    results are always sorted newest-first with unknown-date results kept.
    """
    keys = get_tavily_keys()
    if not keys:
        log.warning("Tavily search skipped — no keys configured")
        return []

    # Tavily hard-caps query length at 400 chars — truncate on a word boundary
    TAVILY_MAX_QUERY_LEN = 400
    if len(query) > TAVILY_MAX_QUERY_LEN:
        truncated = query[:TAVILY_MAX_QUERY_LEN].rsplit(" ", 1)[0]
        log.info("Tavily query truncated: %d → %d chars", len(query), len(truncated))
        query = truncated

    qtype = classify_query(query)
    country = detect_country(query)
    time_range = _detect_recency_time_range(query)
    log.info("Tavily country resolved: %r (qtype=%s, time_range=%r)", country, qtype, time_range)
    t0 = time.perf_counter()

    if len(keys) > 1:
        result_lists = await asyncio.gather(*[
            _tavily_one_call(k, query, max_results, qtype, country, images_out, time_range)
            for k in keys
        ])

        seen_urls: set[str] = set()
        combined_results: list[dict] = []
        for res in result_lists:
            for r in res:
                if r["url"] and r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    r["trusted_source"] = True
                    combined_results.append(r)

        combined = await _enrich_thin_results(combined_results)
        combined = _sort_and_filter_by_freshness(combined, keep_old=historical_intent)
        elapsed = (time.perf_counter() - t0) * 1000
        log.info("Tavily %d-key fan-out done: %d results in %.0fms",
                  len(keys), len(combined), elapsed)
        return combined

    # ── Fallback: only one key configured ────────────────────────────────
    log.info("Tavily search (single-key): query=%r  type=%s  max_results=%d",
              query[:60], qtype, max_results)

    for key in round_robin(keys):
        results = await _tavily_one_call(key, query, max_results, qtype, country, images_out, time_range)
        if results:
            results = await _enrich_thin_results(results)
            results = _sort_and_filter_by_freshness(results, keep_old=historical_intent)
            elapsed = (time.perf_counter() - t0) * 1000
            log.info("Tavily fallback done: %d results in %.0fms", len(results), elapsed)
            return results

    log.error("Tavily: all keys exhausted or failed — trying httpx fallback")
    fallback_results = await _tavily_search_httpx_fallback(query, max_results, qtype, images_out, time_range)
    return _sort_and_filter_by_freshness(fallback_results, keep_old=historical_intent)


async def tavily_search_multi(
    queries: list[str], max_results: int = 20, min_results: int = 10,
    images_out: list | None = None, historical_intent: bool = False,
) -> list[dict]:
    """
    Runs tavily_search() once per query (in parallel) and merges the
    results, deduped by URL. Used whenever query optimization produces more
    than one distinct search intent from a single prompt — one long combined
    query is worse for Tavily than several focused ones.
    Single-query input is a no-op passthrough to tavily_search() — no change
    in behavior for the common case.
    """
    if len(queries) == 1:
        return await tavily_search(
            queries[0], max_results=max_results, min_results=min_results,
            images_out=images_out, historical_intent=historical_intent,
        )

    result_lists = await asyncio.gather(*[
        tavily_search(
            q, max_results=max_results, min_results=min_results,
            images_out=images_out, historical_intent=historical_intent,
        )
        for q in queries
    ])

    seen_urls: set[str] = set()
    merged: list[dict] = []
    for res in result_lists:
        for r in res:
            if r.get("url") and r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                merged.append(r)
    return merged


async def _tavily_search_httpx_fallback(
    query: str, max_results: int, qtype: str, images_out: list | None = None,
    time_range: str | None = None,
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
                    "include_images": images_out is not None,
                    "include_image_descriptions": images_out is not None,
                    "country": "india",
                }
                if qtype == "finance":
                    body["topic"] = "finance"
                if time_range:
                    body["time_range"] = time_range
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
                if images_out is not None:
                    for img in (data.get("images") or []):
                        if isinstance(img, dict):
                            url, desc = img.get("url", ""), img.get("description", "")
                        else:
                            url, desc = str(img), ""
                        if url:
                            images_out.append({"url": url, "description": desc})
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
                    if not _is_junk_domain(r.get("url", ""))
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
        except Exception as e:
            log.debug("Headlines cache not found at %s (%s)", p, e)
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
- **HARD GROUNDING RULE — no fabricated figures:** Every specific number (%, ₹ amount, index level, date, quarter-by-quarter figure, etc.) you state MUST appear verbatim in the WEB SEARCH RESULTS block below. Never invent, estimate, interpolate, or "reconstruct" a plausible-sounding number and attach a source name to it — that is a fabricated citation and is strictly forbidden even if it makes the answer look more complete. If the search results don't contain a number the user is asking for (e.g. a specific quarter's sector return), say plainly that granular figure isn't available in current sources rather than manufacturing one. It is always better to give fewer, verified numbers than to fill gaps with invented ones.
- **QUARTER-BY-QUARTER / PERIOD-BY-PERIOD BREAKDOWNS — a specific failure mode of the rule above:** A request like "sector rotation over the past 8 quarters" is one of the easiest ways to slip into fabrication, because it invites a tidy Q1/Q2/Q3... table even when no source actually contains one. Before writing ANY per-quarter or per-period row (e.g. "Q1 2022: Nifty Auto +15.6%"), check that BOTH the quarter label AND the figure next to it appear together, verbatim, in the WEB SEARCH RESULTS block. If they don't — which is the common case, since ordinary web pages rarely tabulate 8 quarters of sector returns — do NOT construct one anyway. Instead: (a) give the current/most-recent index levels and % changes that the search results DO contain, (b) explain sector rotation and what's driving the current divergence conceptually, and (c) state plainly that a quarter-by-quarter historical breakdown isn't available in current sources. A shorter, honest answer is correct here; a detailed-looking fabricated table is not.
- **"Past N quarters/years" is relative to TODAY ({today}), not to your training data.** Before referencing any quarter or year as "recent" or "the past N quarters," work out the actual calendar/fiscal quarters that span backward from today's date. Do not default to quarters or years that merely feel recent from training — for a request in {today}, quarters from 2022–2023 are roughly 2-3 years stale and are almost never what "past 8 quarters" means.
- Use **bold** for key terms and figures so they're easy to scan.
- Close out market-specific answers with a quick, natural reminder to verify live prices before trading — phrase it like a person would, not a fixed disclaimer line repeated verbatim every time.

**Charts and tables:**
- Default to a markdown table — not prose paragraphs — whenever the answer is a set of 2+ comparable items each with the same few numeric fields: market summaries (index levels + % change), top gainers/losers, multiple stock/fund quotes, earnings figures across companies, etc. This applies even if the user didn't explicitly say "table" — e.g. "latest market news" or "top gainers today" should come back as a table of name/price/change, not a paragraph narrating the same numbers. Add 1-2 sentences of context above or below the table, not instead of it.
- If the user explicitly asks for a chart, graph, plot, or to "visualize" something, give the underlying numbers as a clean markdown table (proper header row + separator row) so it can be rendered as a chart — don't just describe the trend in prose. Use real, distinct numeric values across at least 2 rows/columns; a chart needs actual data points to plot.
- If the user explicitly asks for a table, give exactly one markdown table — don't also restate the same numbers as a bullet list right after it. Pick one format.
- Don't repeat a table's figures again in a separate paragraph below it; reference the table instead (e.g. "as the numbers above show...")."""

    general_persona = f"""You are **Growth Gradual Assistant** — a knowledgeable AI assistant built into the Growth Gradual platform. Today is {today}.

**Voice — sound like a smart, helpful person, not a search-results summarizer:**
- Write in natural, conversational prose. Skip robotic openers like "Based on the search results" — just answer.
- Use bullets or tables only where the content is genuinely list-like or numeric; otherwise prefer well-organized paragraphs.
- Get to the point quickly, then add the supporting detail and nuance.

**Substance:**
- Answer comprehensively using the provided web search results from the top 18 pages.
- Pull in the real numbers, statistics, dates, and figures found in the sources rather than vague generalities.
- When referencing web content, name the publication inline (e.g. "Reuters notes...") — never use bracket-style citation markers like [1] or [1, 2]. That's a hard rule.
- **HARD GROUNDING RULE — no fabricated figures:** Every specific number you state MUST appear verbatim in the WEB SEARCH RESULTS block below. Never invent a plausible number and attribute it to a source. If a figure isn't in the results, say it isn't available rather than making one up.
- This applies especially to period-by-period breakdowns (e.g. "over the past N quarters/years, X was..., then Y was..."): don't construct a tidy timeline unless the specific labels AND figures for each period actually appear in the sources. Give what's genuinely there and say plainly when a granular historical breakdown isn't available, rather than filling the gap with an invented one.
- Use **bold** for key terms where it aids scanning.
- Be thorough, accurate, and helpful. Where relevant, connect the topic back to financial or economic context.

**Charts and tables:**
- Default to a markdown table — not prose paragraphs — whenever the answer is a set of 2+ comparable items each with the same few numeric fields, even if the user didn't explicitly say "table." Add 1-2 sentences of context above or below it, not instead of it.
- If the user explicitly asks for a chart, graph, plot, or to "visualize" something, give the underlying numbers as a clean markdown table (proper header row + separator row) so it can be rendered as a chart — don't just describe the trend in prose.
- If the user explicitly asks for a table, give exactly one markdown table — don't also restate the same numbers as a bullet list right after it."""

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
                        # Without these, a numerically-dense context (lots of
                        # search results full of percentages/index levels)
                        # can push some Llama models into a repetition-loop
                        # collapse — output degenerates into short repeated
                        # tokens like "6: 6: 6: 6:" instead of real text.
                        "frequency_penalty": 0.4,
                        "presence_penalty": 0.3,
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
    # Source: Google AI Studio rate limits dashboard (verified 2026-06-24)
    # gemini-3.1-flash-lite:  15 RPM / 250K TPM / 500 RPD  ← workhorse; highest quota
    # gemini-3.5-flash-lite:  15 RPM / 250K TPM / 500 RPD
    # gemini-3-flash-preview:  5 RPM / 250K TPM /  20 RPD  (API string for "Gemini 3 Flash" in console)
    # gemini-3.6-flash:        5 RPM / 250K TPM /  20 RPD
    # gemini-3.5-flash:        5 RPM / 250K TPM /  20 RPD
    # Removed: gemini-1.5-flash / gemini-1.5-flash-8b (404, retired)
    #          gemini-2.0-flash / gemini-2.0-flash-lite (0/0/0 quota, retired June 2026)
    # Removed 2026-08-11: gemini-2.5-flash / gemini-2.5-flash-lite now return
    # HTTP 404 ("deprecated/unavailable") on every key for this project — see
    # report.py's call_gemini() for the confirming log trace. Swapped in the
    # GA 3.x equivalents (gemini-3.5-flash-lite, gemini-3.6-flash) instead.
    "gemini-3.1-flash-lite",   # 15 RPM / 250K TPM / 500 RPD — highest free quota
    "gemini-3.5-flash-lite",   # 15 RPM / 250K TPM / 500 RPD
    "gemini-3-flash-preview",  #  5 RPM / 250K TPM /  20 RPD
    "gemini-3.6-flash",        #  5 RPM / 250K TPM /  20 RPD — GA default as of July 2026
    "gemini-3.5-flash",        #  5 RPM / 250K TPM /  20 RPD
]

# Models that need explicit thinking suppression to avoid wasting output tokens.
# gemini-3.1-flash-lite defaults to minimal thinking — no param needed.
# Mixing thinkingBudget (2.5) and thinkingLevel (3.x) in one request → HTTP 400.
_GEMINI_THINKING_BUDGET_MODELS = set()  # no 2.5-family models left in this list
_GEMINI_THINKING_LEVEL_MODELS  = {"gemini-3-flash-preview", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"}


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

    # Consecutive-exception circuit breaker per model — a struggling/overloaded
    # model can otherwise eat every key attempt in a row on nothing but read
    # timeouts before the next model in _GEMINI_CHAT_MODELS ever gets a turn.
    consecutive_exceptions: dict[str, int] = {}
    GEMINI_CHAT_MAX_CONSECUTIVE_EXCEPTIONS = 2
    dead_models: set[str] = set()

    for key, model in attempts:
        if model in dead_models:
            continue
        combo = f"{key}:{model}"
        if is_rate_limited(combo):
            continue
        try:
            log.debug("Gemini chat: model=%s key=...%s", model, key[-4:])
            # temperature/top_p/top_k are deprecated on Gemini 3.x (every model
            # left in _GEMINI_CHAT_MODELS) — Google's guidance is to leave them
            # unset; a future model version is documented to error on them
            # rather than silently ignore them, so we don't set one here.
            gen_cfg: dict = {"maxOutputTokens": 2048}
            # Suppress thinking tokens — wastes the 2048-token output budget.
            # Gemini 2.5 uses thinkingBudget; Gemini 3.x uses thinkingLevel.
            # Mixing both params in one request → HTTP 400.
            if model in _GEMINI_THINKING_BUDGET_MODELS:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
            elif model in _GEMINI_THINKING_LEVEL_MODELS:
                gen_cfg["thinkingConfig"] = {"thinkingLevel": "minimal"}
            async with httpx.AsyncClient(timeout=45) as client:
                res = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    json={
                        "system_instruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                        "generationConfig": gen_cfg,
                    },
                )

            # A response came back at all — not a timeout — so reset the
            # streak for this model.
            consecutive_exceptions[model] = 0
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
            # httpx timeout exceptions (ReadTimeout/ConnectTimeout/PoolTimeout)
            # stringify to "" — always include the class name so a bare 45s
            # read timeout doesn't show up as a silent, undiagnosable blank.
            log.warning(
                "Gemini exception model=%s key=...%s: %s%s",
                model, key[-4:], type(exc).__name__,
                f": {exc}" if str(exc) else " (no message — check timeout/network)",
            )
            consecutive_exceptions[model] = consecutive_exceptions.get(model, 0) + 1
            if consecutive_exceptions[model] >= GEMINI_CHAT_MAX_CONSECUTIVE_EXCEPTIONS:
                log.warning(
                    "Gemini chat: model=%s failed %d times in a row with exceptions — "
                    "skipping its remaining keys, moving to next model",
                    model, consecutive_exceptions[model],
                )
                dead_models.add(model)
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

# A repetition-loop collapse streams as a long run of short chunks that are
# purely digits/punctuation ("6", ": ", "6:", "-") with no letters at all —
# real prose never looks like this even mid-word. 10 in a row is well past
# anything a normal response would produce (numbers in real text are always
# interleaved with words), so it's a safe threshold for catching a genuine
# collapse without false-triggering on a report full of percentages.
_DEGENERATE_TOKEN_RE = re.compile(r"^[\d\s:,.\-]{1,6}$")
_DEGENERATE_WINDOW = 10


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

    Also guards against a repetition-loop collapse (seen with Groq's Llama
    models on numerically-dense, search-heavy context): output degenerates
    into short repeated tokens like "6: 6: 6: 6:" instead of real text. We
    hold back a small window of chunks before flushing them downstream, so a
    collapse can be caught and the whole stream aborted (the caller already
    falls back Groq→Gemini on any exception here) BEFORE the garbage ever
    reaches the client — rather than the user watching it stream in live.
    """
    stripper = _CitationStripper()
    leftover = ""
    pending: list[str] = []
    degenerate_streak = 0
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
            if not cleaned:
                continue

            degenerate_streak = degenerate_streak + 1 if _DEGENERATE_TOKEN_RE.match(cleaned) else 0
            if degenerate_streak >= _DEGENERATE_WINDOW:
                log.warning(
                    "Chat: detected repetition-loop collapse (%d numeric/punct-only "
                    "chunks in a row) — aborting before it reaches the client",
                    degenerate_streak,
                )
                raise RuntimeError("degenerate repetition-loop output detected")

            pending.append(cleaned)
            # Only flush once a chunk has survived past the detection window,
            # so a collapse caught above never includes content already sent.
            if len(pending) > _DEGENERATE_WINDOW:
                flushed = pending.pop(0)
                assistant_chunks.append(flushed)
                yield f"data: {json.dumps({'choices': [{'delta': {'content': flushed}}]})}\n\n".encode()

    for flushed in pending:
        assistant_chunks.append(flushed)
        yield f"data: {json.dumps({'choices': [{'delta': {'content': flushed}}]})}\n\n".encode()
    tail = stripper.flush()
    if tail:
        assistant_chunks.append(tail)
        yield f"data: {json.dumps({'choices': [{'delta': {'content': tail}}]})}\n\n".encode()



# ─── Inline chart generation ──────────────────────────────────────────────────
_CHART_SYSTEM_PROMPT = """You are a data extraction specialist for a financial analytics platform.
Given a user question and the assistant's text reply, extract ALL numeric data that can be visualised
as a chart. Return ONLY valid JSON in this exact shape — no markdown fences, no explanation:

{
  "charts": [
    {
      "type": "bar" | "line" | "pie",
      "title": "<specific descriptive title e.g. 'HDFC Bank Net Profit 2020–2024'>",
      "unit": "%" | "₹" | "Cr" | "x" | "",
      "series": [{ "name": "<series name>", "data": [{ "label": "<label>", "value": <number> }] }]
    }
  ]
}

RULES — read carefully:
- Extract ONLY numbers that ACTUALLY APPEAR in the reply text. NEVER invent or estimate values.
- "bar": 3+ named items compared on one metric (e.g. ROE across 5 banks)
- "line": 3+ chronological time points showing a trend (years, quarters, months)
- "pie": 3+ parts that together form a whole (portfolio allocation, segment share)
- Every label within a chart MUST be unique. Every value MUST be a real number (not a string).
- Max 4 charts total. Return {"charts": []} if nothing chartable found.
- For year-on-year data (Net Profit 2020, 2021, 2022, 2023, 2024) → "line" chart
- For cross-sectional comparisons (NPA % of HDFC vs ICICI vs SBI) → "bar" chart
- Minimum data points: line ≥ 3, bar ≥ 3, pie ≥ 3. Never output fewer.
- NEVER duplicate a label within the same series.
- UNIT MUST MATCH THE ACTUAL VALUES BEING CHARTED — this is a hard rule:
  • "unit": "%" is ONLY for values that are themselves percentages (e.g. "6.6% growth", "20% upside") — never apply
    "%" to a rupee figure, a price, a target price, or any other absolute quantity.
  • "unit": "₹" is for rupee amounts (stock prices, target prices, revenue in Rs). NEVER chart a target price
    (e.g. "target price of ₹1,800") with unit "%" just because a percentage also appears nearby in the text.
  • If the reply mentions BOTH a price/target-price AND a percentage for the same items (e.g. "target price ₹1,800,
    20% upside" per stock), pick ONE metric for the chart — prefer the percentage (upside/return/change), since
    that's what's actually comparable across items of different price scales — and set unit="%" accordingly.
    Do not chart the absolute target prices unless the percentage is unavailable; if you do chart prices, unit
    must be "₹", never "%".
  • Before finalizing, re-check every value against its label in the source text: if a number was written as
    "₹1,800" or "Rs. 1,800" in the reply, it is NOT a percentage — it must not end up in a unit="%" chart.
"""

@router.post("/charts")
async def generate_inline_charts(request: Request):
    """
    POST /api/chat/charts
    Body: { question: str, reply: str, queryType?: str }
    Returns: { charts: ChartSpec[] }

    Called by the frontend after each finance bot reply to extract
    and publish charts to Datawrapper (with SVG fallback if token absent).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"charts": []})

    question: str = (body.get("question") or "").strip()
    reply:    str = (body.get("reply")    or "").strip()

    # The user may have explicitly asked for a chart/graph/plot — in that case
    # we should try much harder to produce one, even if the reply text doesn't
    # happen to hit the finance keyword list below and even if it's a bit short.
    explicit_visual_request = bool(re.search(
        r"\b(chart|graph|plot|visuali[sz]e|trend\s*line|pie\s*chart|bar\s*chart)\b",
        question.lower(),
    )) or bool(body.get("wantsVisual"))

    min_len = 40 if explicit_visual_request else 100
    if not reply or len(reply) < min_len:
        return JSONResponse({"charts": []})

    # Only generate charts for finance/data-heavy replies — unless the user
    # explicitly asked for a visual, in which case skip this gate entirely
    # and let the chart-extraction LLM decide what's chartable.
    if not explicit_visual_request:
        q_lower = question.lower() + " " + reply.lower()
        data_keywords = [
            "net profit", "revenue", "roe", "npa", "nim", "market cap", "pe", "eps",
            "return", "growth", "crore", "billion", "%", "percent", "quarter",
            "fy2", "q1", "q2", "q3", "q4", "2020", "2021", "2022", "2023", "2024", "2025",
            "₹", "inr", "basis point", "ratio", "margin", "yield", "nav", "cagr",
        ]
        if not any(k in q_lower for k in data_keywords):
            return JSONResponse({"charts": []})

    user_prompt = (
        f"USER QUESTION: {question[:500]}\n\n"
        f"ASSISTANT REPLY (extract charts from this):\n{reply[:4000]}\n\n"
        "Return ONLY a JSON array of chart specs. Return [] if nothing chartable."
    )

    keys = get_groq_keys()
    raw_json = ""
    for key in round_robin(keys):
        if is_rate_limited(key):
            continue
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": _CHART_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 2500,
                        "temperature": 0.1,
                        # json_object mode: works with our object-shaped prompt {"charts":[...]}
                        "response_format": {"type": "json_object"},
                    },
                )
            if res.is_success:
                raw_json = res.json()["choices"][0]["message"]["content"]
                break
            elif res.status_code == 429:
                mark_rate_limited(key, 60_000)
        except Exception as exc:
            log.debug("Chart gen Groq error: %s", exc)

    if not raw_json:
        return JSONResponse({"charts": []})

    # Parse — LLM returns {"charts": [...]} due to json_object + our prompt shape
    try:
        parsed = json.loads(raw_json)
        if isinstance(parsed, dict):
            # Primary: {"charts": [...]}
            charts = parsed.get("charts") or parsed.get("data") or []
            # Fallback: dict is itself a single chart spec
            if not charts and parsed.get("series"):
                charts = [parsed]
        elif isinstance(parsed, list):
            # Shouldn't happen with json_object mode, but handle it safely
            charts = parsed
        else:
            charts = []
    except Exception as exc:
        log.debug("Chart JSON parse failed: %s — raw: %s", exc, raw_json[:200])
        return JSONResponse({"charts": []})

    # Validate each chart — drop obviously bad ones
    valid = []
    for c in charts:
        if not isinstance(c, dict):
            continue
        series = c.get("series") or []
        all_pts = [pt for s in series for pt in (s.get("data") or [])]
        if len(all_pts) < 2:
            continue
        vals = [pt.get("value") for pt in all_pts if pt.get("value") is not None]
        labels = [pt.get("label", "") for pt in all_pts]
        if len(set(vals)) < 2 or len(set(labels)) < 2:
            continue
        # Sanity check: unit="%" with values far outside any plausible
        # percentage range (e.g. a ₹1,800 target price mislabeled as "%")
        # means the LLM almost certainly mismatched a price column with the
        # percentage unit. A single-day/period % move or upside figure is
        # essentially never above ~200% — bail on the chart rather than ship
        # a misleading "+1800.00%" bar.
        if c.get("unit") == "%" and any(abs(v) > 300 for v in vals if isinstance(v, (int, float))):
            log.warning(
                "Inline chart dropped — unit='%%' but values look like prices, not percentages: %r",
                vals,
            )
            continue
        valid.append(c)

    if not valid:
        return JSONResponse({"charts": []})

    # Publish to Datawrapper (no-op if token absent — returns unchanged)
    try:
        from utils.datawrapper import attach_datawrapper_charts
        valid = await attach_datawrapper_charts(valid, fetch_png_bytes=False)
    except Exception as exc:
        log.debug("Datawrapper attach failed (non-critical): %s", exc)

    log.info("Inline charts: generated %d chart(s) for question=%r", len(valid), question[:60])
    return JSONResponse({"charts": valid})


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

    # ── Query optimization: deterministic rules for normal prompts, LLM
    # rewrite reserved for genuinely ambiguous follow-ups (see
    # rewrite_query_for_search / _is_ambiguous_followup) ──
    # Run only when search or RAG will actually use the query (saves work otherwise)
    search_queries = [last_user_msg]
    if do_search or has_rag:
        # Pass history excluding the current (last) user message so context is prior turns
        prior_history = messages[:-1] if messages and messages[-1].get("role") == "user" else messages
        search_queries = await rewrite_query_for_search(last_user_msg, prior_history, qtype=qtype)
    search_query = search_queries[0]  # representative query, used for RAG + logging

    log.info(
        "Chat request: msg=%r  search_query=%r  search=%s  type=%s  file_ctx=%s  rag=%s  smalltalk=%s  session=%s",
        last_user_msg[:80], search_query[:80] if search_query != last_user_msg else "(unchanged)",
        do_search, qtype, bool(file_context), has_rag, is_smalltalk_msg, session_id or "none",
    )
    if len(search_queries) > 1:
        log.info("Chat: multi-intent prompt split into %d queries: %r",
                  len(search_queries), [q[:60] for q in search_queries])

    # Upsert the session row upfront (fire-and-forget — don't block the stream)
    if session_id:
        asyncio.ensure_future(_upsert_session(session_id))

    async def _no_search() -> list:
        return []

    async def _no_headlines() -> str:
        return ""

    _historical_intent = bool(_QUERY_HISTORICAL_RE.search(last_user_msg))
    search_results, headlines = await asyncio.gather(
        tavily_search_multi(
            search_queries, max_results=20, min_results=10,
            historical_intent=_historical_intent,
        ) if do_search else _no_search(),
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
