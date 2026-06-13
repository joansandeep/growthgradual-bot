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

# ─── Domain lists ──────────────────────────────────────────────────────────────
FINANCE_DOMAINS = [
    "moneycontrol.com", "economictimes.indiatimes.com", "livemint.com",
    "business-standard.com", "ndtvprofit.com", "cnbctv18.com",
    "financialexpress.com", "thehindubusinessline.com", "bseindia.com", "nseindia.com",
    "valueresearchonline.com", "cafemutual.com", "capitalmarket.com",
    "equitymaster.com", "stockanalysis.com", "screener.in", "tickertape.in",
    "zeebiz.com", "outlookbusiness.com",
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com",
    "marketwatch.com", "investing.com", "tradingeconomics.com",
    "rbi.org.in", "sebi.gov.in", "amfiindia.com",
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


async def tavily_search(query: str, max_results: int = 25) -> list[dict]:
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

                # Retry without domain filter if too few results
                if len(results) < 5:
                    log.info("Tavily: only %d results with domain filter — retrying without", len(results))
                    res2 = await client.post("https://api.tavily.com/search", json={
                        "api_key": key,
                        "query": query,
                        "search_depth": "advanced",
                        "max_results": max_results,
                        "include_raw_content": True,
                    })
                    if res2.is_success:
                        results = [
                            {
                                "title": r.get("title", ""),
                                "url": r.get("url", ""),
                                "snippet": r.get("content", ""),
                                "fullContent": (r.get("raw_content") or r.get("content") or "")[:5000],
                                "score": r.get("score"),
                                "published": r.get("published_date"),
                            }
                            for r in (res2.json().get("results") or [])
                        ]
                        results = [_clean_result_content(r) for r in results]

                # Enrich top-10 sparse results
                enriched = []
                async def _passthrough(val: str) -> str:
                    return val

                enrich_tasks = [
                    fetch_page_content(r["url"], 3000)
                    if len(r.get("fullContent", "")) < 500 else _passthrough(r.get("fullContent", ""))
                    for r in results[:10]
                ]
                extra_contents = await asyncio.gather(*enrich_tasks, return_exceptions=True)
                for i, r in enumerate(results[:10]):
                    extra = extra_contents[i] if not isinstance(extra_contents[i], Exception) else ""
                    if isinstance(extra, str) and len(extra) > len(r.get("fullContent", "")):
                        r["fullContent"] = extra
                    enriched.append(r)

                elapsed = (time.perf_counter() - t0) * 1000
                log.info("Tavily done: %d results in %.0fms", len(results), elapsed)
                return enriched + results[10:]

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
        for i, r in enumerate(search_results[:25]):
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
- Answer comprehensively using the provided web search results from the top 25 pages.
- Extract and present ALL specific numbers, statistics, data points, dates, and figures found in the sources.
- Cite sources by [number] when referencing web content.
- Use markdown for clarity: **bold** key terms, bullet lists, tables where helpful.
- Be thorough, accurate, and helpful. Where relevant, connect the topic back to financial or economic context."""

    base = finance_persona if qtype == "finance" else general_persona
    return base + headlines + web_ctx


# ─── Groq streaming ────────────────────────────────────────────────────────────
async def stream_groq(system_prompt: str, messages: list[dict]) -> AsyncGenerator[str, None] | None:
    """
    Try every Groq key in round-robin order.
    Returns an async generator that yields SSE chunks on success,
    or None if all keys are exhausted / unavailable.
    """
    keys = get_groq_keys()
    if not keys:
        log.warning("Groq: no keys configured")
        return None

    log.info("Groq: attempting stream with %d key(s)", len(keys))
    for key in round_robin(keys):
        if is_rate_limited(key):
            log.debug("Groq: skipping rate-limited key ...%s", key[-4:])
            continue
        try:
            log.debug("Groq: trying key ...%s", key[-4:])
            async with httpx.AsyncClient(timeout=90) as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {key}",
                    },
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [{"role": "system", "content": system_prompt}] + messages,
                        "stream": True,
                        "max_tokens": 2048,
                        "temperature": 0.5,
                    },
                )

            if res.status_code == 429:
                log.warning("Groq 429 on key ...%s — rotating", key[-4:])
                mark_rate_limited(key, 60_000)
                continue
            if res.status_code in (401, 403):
                log.warning("Groq %d on key ...%s — banned for 24h", res.status_code, key[-4:])
                mark_rate_limited(key, 24 * 60 * 60_000)
                continue
            if not res.is_success:
                log.warning("Groq HTTP %d on key ...%s", res.status_code, key[-4:])
                continue

            raw = res.content
            try:
                maybe_err = json.loads(raw)
                err_code = (maybe_err.get("error") or {}).get("code", "")
                if err_code in ("rate_limit_exceeded", "tokens_exhausted", "quota_exceeded"):
                    log.warning("Groq quota/rate error '%s' on key ...%s", err_code, key[-4:])
                    mark_rate_limited(key, 60_000)
                    continue
            except Exception:
                pass  # binary / streaming body — not a JSON error blob

            log.info("Groq: streaming started with key ...%s", key[-4:])

            async def _replay_and_stream(
                initial: bytes,
                response: httpx.Response,
            ) -> AsyncGenerator[str, None]:
                yield initial.decode(errors="replace")
                async for chunk in response.aiter_bytes():
                    decoded = chunk.decode(errors="replace")
                    if '"rate_limit_exceeded"' in decoded or '"tokens_exhausted"' in decoded:
                        log.warning("Groq: mid-stream token exhaustion detected — stopping")
                        return
                    yield decoded

            return _replay_and_stream(raw, res)

        except Exception as exc:
            log.warning("Groq exception on key ...%s: %s", key[-4:], exc)
            continue

    log.error("Groq: all keys exhausted")
    return None


_GEMINI_CHAT_MODELS = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.0-flash-lite"]


async def gemini_sse(system_prompt: str, messages: list[dict]) -> AsyncGenerator[str, None]:
    keys = get_gemini_keys()
    if not keys:
        raise ValueError("No Gemini keys")

    user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    available_keys = [k for k in round_robin(keys) if not is_rate_limited(k)] or keys
    attempts = [(k, m) for m in _GEMINI_CHAT_MODELS for k in available_keys]

    log.info("Gemini chat: %d key(s) × %d models = %d attempts", len(available_keys), len(_GEMINI_CHAT_MODELS), len(attempts))

    for key, model in attempts:
        if is_rate_limited(f"{key}:{model}"):
            continue
        try:
            log.debug("Gemini chat: model=%s key=...%s", model, key[-4:])
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    json={
                        "system_instruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.5},
                    },
                )
            if res.status_code == 503:
                log.warning("Gemini 503 model=%s key=...%s", model, key[-4:])
                mark_rate_limited(f"{key}:{model}", 30_000)
                continue
            if res.status_code == 429:
                log.warning("Gemini 429 model=%s key=...%s", model, key[-4:])
                mark_rate_limited(key, 60_000)
                mark_rate_limited(f"{key}:{model}", 60_000)
                continue
            if res.status_code == 403:
                log.warning("Gemini 403 key=...%s — banned 24h", key[-4:])
                mark_rate_limited(key, 24 * 60 * 60_000)
                continue
            data = res.json()
            text = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if not text:
                log.warning("Gemini: empty response model=%s key=...%s", model, key[-4:])
                continue
            log.info("Gemini chat: got %d chars model=%s key=...%s", len(text), model, key[-4:])
            for word in text.split(" "):
                yield f'data: {json.dumps({"choices": [{"delta": {"content": word + " "}}]})}\n\n'
            yield "data: [DONE]\n\n"
            return
        except Exception as exc:
            log.warning("Gemini exception model=%s key=...%s: %s", model, key[-4:], exc)
            continue
    raise RuntimeError("All Gemini key×model combinations failed")


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
    if not messages:
        return JSONResponse({"error": "No messages"}, status_code=400)

    last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    do_search = needs_web_search(last_user_msg)
    qtype = classify_query(last_user_msg)

    log.info(
        "Chat request: msg=%r  search=%s  type=%s  file_ctx=%s",
        last_user_msg[:80], do_search, qtype, bool(file_context),
    )

    async def _no_search() -> list:
        return []

    search_results, headlines = await asyncio.gather(
        tavily_search(last_user_msg, 25) if do_search else _no_search(),
        load_headlines(30),
    )

    base_prompt = build_system(headlines, search_results, qtype)
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
        yield meta_event.encode()

        # ── Try Groq (all keys, round-robin) ──────────────────────────────────
        groq_gen = await stream_groq(system_prompt, messages)
        if groq_gen is not None:
            try:
                async for chunk in groq_gen:
                    yield chunk.encode() if isinstance(chunk, str) else chunk
                elapsed = (time.perf_counter() - t0) * 1000
                log.info("Chat complete via Groq in %.0fms", elapsed)
                return
            except Exception as exc:
                log.warning("Groq stream broke mid-way: %s — falling back to Gemini", exc)

        # ── Gemini fallback (all keys, round-robin) ───────────────────────────
        gemini_keys = get_gemini_keys()
        if gemini_keys:
            try:
                async for chunk in gemini_sse(system_prompt, messages):
                    yield chunk.encode()
                elapsed = (time.perf_counter() - t0) * 1000
                log.info("Chat complete via Gemini in %.0fms", elapsed)
                return
            except Exception as e:
                log.error("All LLM providers failed: %s", e)
                err = json.dumps({"choices": [{"delta": {"content": f"\n\n[All LLM keys exhausted — please retry] {e}"}}]})
                yield f"data: {err}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return

        log.error("Chat: no LLM keys configured at all")
        yield b'data: {"choices":[{"delta":{"content":"No LLM keys configured"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
