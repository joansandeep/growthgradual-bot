"""
POST /api/chat  — SSE streaming chat
Body: { messages: [{role, content}], fileContext?: string }
"""
import asyncio
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
def classify_query(msg: str) -> str:
    m = msg.lower()
    return "finance" if any(t in m for t in FINANCE_TERMS) else "general"


def needs_web_search(msg: str) -> bool:
    m = msg.lower().strip()
    # Only skip search for empty inputs or trivial one-word greetings
    if len(m) < 4:
        return False
    TRIVIAL = {"hi", "hey", "hello", "ok", "okay", "thanks", "thank you", "bye", "yes", "no", "sure", "great"}
    if m in TRIVIAL:
        return False
    # Everything else gets a web search
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


async def tavily_search(query: str, max_results: int = 25, min_results: int = 18) -> list[dict]:
    keys = get_tavily_keys()
    if not keys:
        log.warning("Tavily search skipped — no keys configured")
        return []

    qtype = classify_query(query)
    domains = FINANCE_DOMAINS if qtype == "finance" else GENERAL_DOMAINS
    log.info("Tavily search: query=%r  type=%s  max_results=%d", query[:60], qtype, max_results)
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
                }
                if qtype == "finance":
                    body["topic"] = "finance"

                res = await client.post("https://api.tavily.com/search", json=body)
                if res.status_code == 429:
                    log.warning("Tavily 429 — marking key rate-limited")
                    mark_rate_limited(key, 60_000)
                    continue
                if res.status_code in (401, 403):
                    log.warning("Tavily %d — marking key banned for 24h", res.status_code)
                    mark_rate_limited(key, 24 * 60 * 60_000)
                    continue
                if not res.is_success:
                    log.warning("Tavily HTTP %d", res.status_code)
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
                results = [_clean_result_content(r) for r in results]

                # Domain filter is always kept — fewer than min_results is acceptable
                if len(results) < min_results:
                    log.info("Tavily: %d results with domain filter (target min=%d) — keeping domain filter", len(results), min_results)

                # Enrich top-10 sparse results
                enriched = []
                async def _passthrough(val: str) -> str:
                    return val

                enrich_tasks = [
                    fetch_page_content(r["url"], 3000)
                    if len(r.get("fullContent", "")) < 500 else _passthrough(r.get("fullContent", ""))
                    for r in results[:18]
                ]
                extra_contents = await asyncio.gather(*enrich_tasks, return_exceptions=True)
                for i, r in enumerate(results[:18]):
                    extra = extra_contents[i] if not isinstance(extra_contents[i], Exception) else ""
                    if isinstance(extra, str) and len(extra) > len(r.get("fullContent", "")):
                        r["fullContent"] = extra
                    enriched.append(r)

                elapsed = (time.perf_counter() - t0) * 1000
                log.info("Tavily done: %d results in %.0fms", len(results), elapsed)
                return enriched + results[18:]

            except Exception as exc:
                log.warning("Tavily exception: %s", exc)
                continue

    log.error("Tavily: all keys exhausted or failed")
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


def build_system(headlines: str, search_results: list[dict], qtype: str) -> str:
    from datetime import date
    today = date.today().strftime("%A, %B %d, %Y")
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
            snippets.append(f"[{i+1}] {r['title']}\nSource: {r['url']}{pub}\n{content}")
        web_ctx = f"\n\n---\n🌐 WEB SEARCH RESULTS — TOP {len(search_results)} PAGES (with full page content):\n\n" + "\n\n".join(snippets) + "\n---"

    finance_persona = f"""You are **Growth Gradual** — an expert AI assistant for Indian financial markets, built into the Growth Gradual "In The Money" platform. Today is {today}.

You specialise in NSE/BSE stocks, IPOs, mutual funds, RBI/SEBI policy, macroeconomics, and personal finance for Indian investors.

**Behaviour:**
- Give sharp, specific, data-driven answers. Cite sources by [number] when using web results.
- When web results are provided, extract and use ALL numbers, percentages, dates, and figures from them.
- Use markdown: **bold** key terms, bullet lists, tables where helpful.
- Always end market-specific answers with "Verify live prices before trading."
- Be conversational but precise — like a top sell-side analyst."""

    general_persona = f"""You are **Growth Gradual Assistant** — a knowledgeable AI assistant built into the Growth Gradual platform. Today is {today}.

**Behaviour:**
- Answer comprehensively using the provided web search results from the top 18 pages.
- Extract and present ALL specific numbers, statistics, data points, dates, and figures found in the sources.
- Cite sources by [number] when referencing web content.
- Use markdown for clarity: **bold** key terms, bullet lists, tables where helpful.
- Be thorough, accurate, and helpful. Where relevant, connect the topic back to financial or economic context."""

    base = finance_persona if qtype == "finance" else general_persona
    return base + headlines + web_ctx


# ─── Groq streaming ────────────────────────────────────────────────────────────
_GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama3-70b-8192",
    "llama3-8b-8192",
    "gemma2-9b-it",
    "mixtral-8x7b-32768",
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
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
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


# ─── SSE chunk → text extraction ──────────────────────────────────────────────
def _extract_text_from_chunk(raw: str) -> str:
    """Pull assistant text out of an OpenAI-style SSE chunk string."""
    text_parts = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("[DONE]", ""):
            continue
        try:
            obj = json.loads(payload)
            # Skip meta events
            if obj.get("type") == "meta":
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {})
            text_parts.append(delta.get("content", ""))
        except Exception:
            pass
    return "".join(text_parts)


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
    has_rag:      bool = bool(body.get("hasRag", False))
    # sessionId is optional — only persists when provided
    session_id: str = (body.get("sessionId") or "").strip()

    if not messages:
        return JSONResponse({"error": "No messages"}, status_code=400)

    last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    do_search = needs_web_search(last_user_msg)
    qtype = classify_query(last_user_msg)

    log.info(
        "Chat request: msg=%r  search=%s  type=%s  file_ctx=%s  rag=%s  session=%s",
        last_user_msg[:80], do_search, qtype, bool(file_context), has_rag, session_id or "none",
    )

    # Upsert the session row upfront (fire-and-forget — don't block the stream)
    if session_id:
        asyncio.ensure_future(_upsert_session(session_id))

    async def _no_search() -> list:
        return []

    search_results, headlines = await asyncio.gather(
        tavily_search(last_user_msg, max_results=25, min_results=18) if do_search else _no_search(),
        load_headlines(30),
    )

    base_prompt = build_system(headlines, search_results, qtype)

    # ── RAG grounding: use RAG service system prompt when files are indexed ───
    rag_system_prompt = ""
    if has_rag and session_id:
        log.info("Chat: RAG mode — querying for session %s question=%r", session_id[:8], last_user_msg[:60])
        rag_result = await _rag_query(
            session_id=session_id,
            question=last_user_msg,
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
        # RAG takes full control of the system prompt — ignore web search
        system_prompt = rag_system_prompt
    else:
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
                async for chunk in groq_gen:
                    raw = chunk if isinstance(chunk, str) else chunk.decode(errors="replace")
                    assistant_chunks.append(_extract_text_from_chunk(raw))
                    yield raw.encode() if isinstance(chunk, str) else chunk
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
                async for chunk in gemini_sse(system_prompt, messages):
                    raw = chunk if isinstance(chunk, str) else chunk.decode(errors="replace")
                    assistant_chunks.append(_extract_text_from_chunk(raw))
                    yield chunk.encode() if isinstance(chunk, str) else chunk
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
