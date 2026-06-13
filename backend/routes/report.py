"""
POST /api/chat/report  — Generate comprehensive research report (JSON)
Body: { question: str, sources: [{title, url, snippet, fullContent?}] }
"""
import asyncio
import json
import logging
import re
import time

import httpx
from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from utils.keys import (
    get_gemini_keys, get_groq_keys,
    is_rate_limited, mark_rate_limited, round_robin
)

router = APIRouter()
log = logging.getLogger("report")

SKIP_PAGE_FETCH = [".pdf", "bloomberg.com", "wsj.com", "ft.com", "economist.com"]

SYSTEM_PROMPT = """You are a senior research analyst at Growth Gradual. Write a COMPREHENSIVE, well-structured research report on ANY topic using the scraped web sources provided.
You MUST respond with valid JSON only — no markdown fences, no preamble, no text outside JSON.

Respond with EXACTLY this shape:
{
  "title": "<concise report title, max 12 words — a noun-phrase headline like 'Global Growth Slowdown and AI Investment Trends', NEVER a sentence starting with 'Here are', 'Based on', 'The following', or similar preamble/instruction phrasing>",
  "report": "<full markdown report — target 800-1200 words, structured and data-rich>",
  "charts": [...],
  "keyStats": [{ "label": "<short label>", "value": "<value string>", "change": "<+/- % or empty string>" }],
  "summary": "<2-3 sentence executive summary>"
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHART RULES — READ CAREFULLY. VIOLATIONS = BROKEN UI.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — SCAN sources for chartable data:
  Look for: tables, rankings, comparisons, time-series, percentages, prices, volumes, growth rates.
  For each chartable dataset you find, decide the BEST chart type:
    • bar  → named items compared (stocks, sectors, companies, countries) — need ≥3 items with distinct values
    • line → data over time (dates, quarters, years, months) — need ≥4 time points with DISTINCT values
    • pie  → parts of a whole (portfolio breakdown, market share, sector allocation) — need ≥3 distinct slices

STEP 2 — ONLY create a chart if ALL conditions are met:
  ✓ At least 3 data points (bar/pie) or 4 time points (line) — charts with fewer points will be SILENTLY DROPPED from the PDF
  ✓ All labels are DIFFERENT from each other — NEVER repeat a label
  ✓ All values are DIFFERENT from each other — NOT all the same number
  ✓ Values come VERBATIM from the source — NEVER invented, estimated, or calculated
  ✓ The source explicitly states each individual data point — NOT inferred from a single current value
  ✗ If these conditions cannot be met → DO NOT create the chart at all
  ✗ NEVER create a line chart showing historical price levels you calculated or estimated
  ✗ NEVER create a chart from a single number (e.g. "Sensex is at 74,503" is ONE data point — not chartable)
  ✗ A bar chart needs ≥3 NAMED items (e.g. 3 different sector indices) — not 1 item shown 3 ways

STEP 3 — Place [CHART_n] inline in the report markdown exactly where each chart should appear — right after the paragraph whose data it visualises. Number from 1. charts[0] = [CHART_1], charts[1] = [CHART_2], etc.

STEP 4 — Number of charts: 0 to 6. Purely driven by what real data exists. Do NOT pad to any minimum. Do NOT chart the same data twice.

Chart spec shape:
{
  "type": "bar" | "line" | "pie",
  "title": "<specific descriptive title, e.g. 'Nifty Sector Returns Today' not 'Chart 1'>",
  "unit": "%" | "₹" | "Cr" | "B" | "$" | "x" | "",
  "series": [{ "name": "<series name>", "data": [{ "label": "<unique label>", "value": <number> }] }]
}

GOOD chart examples — do exactly this:
  • Nifty 50 Top Gainers → bar, labels=stock names, values=% change each stock, unit="%"
  • Sector Performance → bar, labels=[Nifty Bank, Nifty IT, Nifty Auto, Nifty FMCG], values=% change, unit="%"
  • Crude Oil Prices Last Week → line, labels=[Mon, Tue, Wed, Thu, Fri], values=USD per barrel
  • FII vs DII Net Flows → bar, labels=[Mon, Tue, Wed, Thu, Fri], values=₹ crore, unit="Cr"
  • Mutual Fund Category Inflows → pie, labels=category names, values=₹ crore inflow each

BAD chart examples — NEVER do this:
  ✗ labels=["Today","Today","Today"] — duplicate labels, meaningless
  ✗ values=[0.5, 0.5, 0.6, 0.6] — nearly identical, useless visually
  ✗ "Market Projection" with future values you invented — not from source
  ✗ Line chart with only 2 data points — use a bar chart instead
  ✗ [CHART_n] in report without a matching charts[n-1] entry, or vice versa

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPORT STRUCTURE (each section 100-200 words — be concise, data-dense, no padding):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# [Report Title]

## 1. Introduction
2-3 paragraphs: context, key stakeholders, what happened today. Cite [n]. No filler.

## 2. Data Sources & Methodology
One markdown table of sources (Publication | URL | Data type). 1 short paragraph on approach.

## 3. Data Analysis

### 3.1 [Heading that matches the actual topic and data]
Deep-dive quantitative findings. ALL numbers in markdown tables. Insert [CHART_n] immediately after the paragraph whose data it visualises — only if a valid chart exists for that data.

### 3.2 [Second dimension of analysis relevant to topic]
Comparisons, breakdowns, benchmarks. Tables + [CHART_n] only where valid distinct data exists.

### 3.3 [Third dimension — trends or forward-looking data if sources contain it]
Only include this section if sources have trend/time-series data. Insert [CHART_n] only if ≥4 real distinct time points exist.

## 4. Key Findings
8-10 numbered findings, each with a specific number/stat from sources. Cite [n] on every finding.

## 5. Conclusion
2 paragraphs: synthesise findings + forward outlook from sources only.

## 6. References
[n] Publication. Title. URL (date if known)

GLOBAL RULES:
- NEVER invent any number, date, name, or statistic
- TABLE HYGIENE: every column in a table must have real values for EVERY row. If a column would
  be "-" or empty for some rows (e.g. indices like NIFTY 50/NIFTY BANK have no market cap),
  do NOT include that column for those rows — instead, put indices and individual stocks in
  SEPARATE tables (one for index levels, one for stock market caps), or drop the column entirely
  if it doesn't apply to the row type being shown.
- Cite [n] throughout every section
- At least 3 markdown data tables
- keyStats: 6-8 real metrics with values and change indicators
- Target 800-1200 words total — quality over quantity, every sentence must add data or insight
"""


async def fetch_page_content(url: str, max_chars: int = 6000) -> str:
    if any(s in url for s in SKIP_PAGE_FETCH):
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            }, follow_redirects=True)
            if not res.is_success:
                return ""
            html = res.text

            chart_snippets = []
            for m in re.finditer(r"<script[^>]*>([\s\S]*?)</script>", html, re.IGNORECASE):
                s = m.group(1)
                if any(k in s for k in ("labels", "categories", "xAxis")) and \
                   any(k in s for k in ("data", "series", "values")) and \
                   len(s) < 8000:
                    chart_snippets.append(s[:1000])

            clean = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
            clean = re.sub(r"<style[\s\S]*?</style>", "", clean, flags=re.IGNORECASE)
            clean = re.sub(r"<[^>]+>", " ", clean)
            clean = re.sub(r"\s{3,}", "\n", clean)
            for esc, rep in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")]:
                clean = clean.replace(esc, rep)
            clean = clean.strip()

            chart_ctx = ""
            if chart_snippets:
                joined = "\n".join(chart_snippets[:3])[:1500]
                chart_ctx = f"\n[CHART DATA FOUND ON PAGE]:\n{joined}\n"

            return clean[: max_chars - len(chart_ctx)] + chart_ctx
    except Exception as exc:
        log.debug("fetch_page_content failed for %s: %s", url, exc)
        return ""


# Groq context limit: ~6K tokens input. Trim prompt to avoid 413.
GROQ_MAX_PROMPT_CHARS = 7_000   # safe hard cap — 9K+ reliably triggers 413

def _trim_for_groq(prompt: str) -> str:
    """Hard-cap the user prompt so Groq never returns 413 Payload Too Large."""
    if len(prompt) <= GROQ_MAX_PROMPT_CHARS:
        return prompt
    # Keep the instruction header (first 400 chars) + trimmed sources
    header_end = prompt.find("Scraped content")
    if header_end == -1:
        return prompt[:GROQ_MAX_PROMPT_CHARS]
    header = prompt[:header_end + 100]
    body   = prompt[header_end + 100:]
    allowed = GROQ_MAX_PROMPT_CHARS - len(header) - 200
    trimmed_body = body[:allowed]
    # Trim at last clean source boundary so we don't cut mid-sentence
    last_sep = trimmed_body.rfind("\n---\n")
    if last_sep > allowed * 0.5:
        trimmed_body = trimmed_body[:last_sep]
    footer = "\n\nINSTRUCTIONS:\n1. Extract ALL numbers, tables, percentages verbatim.\n2. Follow CHART RULES exactly.\n3. Respond ONLY with the JSON object."
    result = header + trimmed_body + footer
    log.info("Groq: trimmed prompt %d→%d chars to avoid 413", len(prompt), len(result))
    return result


async def call_groq(user_prompt: str) -> str:
    keys = get_groq_keys()
    if not keys:
        log.warning("Groq: no keys configured for report generation")
        return ""

    trimmed_prompt = _trim_for_groq(user_prompt)
    log.info("Groq: calling for report generation with %d key(s)  prompt=%d chars", len(keys), len(trimmed_prompt))

    for key in round_robin(keys):
        if is_rate_limited(key):
            log.debug("Groq: skipping rate-limited key ...%s", key[-4:])
            continue
        try:
            log.debug("Groq: trying key ...%s", key[-4:])
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=90) as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": trimmed_prompt},
                        ],
                        "max_tokens": 8000,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
            if res.status_code == 413:
                log.warning("Groq 413 on key ...%s — retrying with harder trim", key[-4:])
                # Try once more at half the current limit before giving up
                harder_trim = len(trimmed_prompt) // 2
                trimmed_prompt = trimmed_prompt[:harder_trim]
                try:
                    async with httpx.AsyncClient(timeout=90) as client2:
                        res2 = await client2.post(
                            "https://api.groq.com/openai/v1/chat/completions",
                            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                            json={
                                "model": "llama-3.3-70b-versatile",
                                "messages": [
                                    {"role": "system", "content": SYSTEM_PROMPT},
                                    {"role": "user", "content": trimmed_prompt},
                                ],
                                "max_tokens": 8000,
                                "temperature": 0.2,
                                "response_format": {"type": "json_object"},
                            },
                        )
                    if res2.is_success:
                        text2 = res2.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                        if text2:
                            log.info("Groq: report generated on retry (%d chars)", len(text2))
                            return text2
                except Exception:
                    pass
                # Still failing — skip Groq entirely
                for k in keys:
                    mark_rate_limited(k, 120_000)
                return ""
            if res.status_code == 429:
                log.warning("Groq 429 on key ...%s", key[-4:])
                mark_rate_limited(key, 60_000)
                continue
            if res.status_code in (401, 403):
                log.warning("Groq %d on key ...%s — banned for 24h", res.status_code, key[-4:])
                mark_rate_limited(key, 24 * 60 * 60_000)
                continue
            if not res.is_success:
                log.warning("Groq HTTP %d on key ...%s", res.status_code, key[-4:])
                continue
            try:
                data = res.json()
                err_code = (data.get("error") or {}).get("code", "")
                if err_code in ("rate_limit_exceeded", "tokens_exhausted", "quota_exceeded"):
                    log.warning("Groq quota error '%s' on key ...%s", err_code, key[-4:])
                    mark_rate_limited(key, 60_000)
                    continue
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception as exc:
                log.warning("Groq: failed to parse response: %s", exc)
                continue
            if text:
                elapsed = (time.perf_counter() - t0) * 1000
                log.info("Groq: report generated in %.0fms (%d chars)", elapsed, len(text))
                return text
        except Exception as exc:
            log.warning("Groq exception on key ...%s: %s", key[-4:], exc)
            continue

    log.error("Groq: all keys exhausted for report generation")
    return ""


# Gemini model priority: try best model first, fall back on 503/429/404
GEMINI_MODELS = [
    "gemini-2.5-flash",        # best quality — try first
    "gemini-2.0-flash",        # reliable fallback (replaced deprecated 1.5-flash)
    "gemini-2.5-flash-8b",     # lighter/faster — good for overload situations
    "gemini-2.0-flash-lite",   # last resort
]


async def call_gemini(user_prompt: str) -> str:
    keys = get_gemini_keys()
    if not keys:
        log.warning("Gemini: no keys configured for report generation")
        return ""

    log.info("Gemini: attempting report generation with %d key(s), %d models", len(keys), len(GEMINI_MODELS))

    # Build a flat attempt list: (key, model) — cycle through all key×model combos
    # Priority: try each model with the least-used key first, then next model, etc.
    available_keys = [k for k in round_robin(keys) if not is_rate_limited(k)]
    if not available_keys:
        available_keys = keys  # all rate-limited — try anyway as last resort

    # Build attempt queue: [(key, model), ...] — model varies slowest
    attempts = []
    for model in GEMINI_MODELS:
        for key in available_keys:
            attempts.append((key, model))

    for key, model in attempts:
        if is_rate_limited(f"{key}:{model}"):
            continue
        try:
            log.debug("Gemini: trying model=%s key=...%s", model, key[-4:])
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=90) as client:
                res = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    json={
                        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                        "generationConfig": {
                            "maxOutputTokens": 16000,
                            "temperature": 0.1,
                            "responseMimeType": "application/json",  # force JSON — avoids markdown fences
                        },
                    },
                )
            if res.status_code == 503:
                log.warning("Gemini 503 (overloaded) model=%s key=...%s — trying next", model, key[-4:])
                mark_rate_limited(f"{key}:{model}", 30_000)  # back off this key+model for 30s
                continue
            if res.status_code == 429:
                log.warning("Gemini 429 on model=%s key=...%s", model, key[-4:])
                mark_rate_limited(key, 60_000)
                mark_rate_limited(f"{key}:{model}", 60_000)
                continue
            if res.status_code == 403:
                log.warning("Gemini 403 on key=...%s — banned for 24h", key[-4:])
                mark_rate_limited(key, 24 * 60 * 60_000)
                continue
            if not res.is_success:
                log.warning("Gemini HTTP %d on model=%s key=...%s", res.status_code, model, key[-4:])
                if res.status_code == 404:
                    # Model doesn't exist — no point trying other keys for this model
                    log.warning("Gemini 404 — model=%s is deprecated/unavailable, skipping all keys for it", model)
                    break
                continue
            text = res.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if text:
                elapsed = (time.perf_counter() - t0) * 1000
                log.info("Gemini: report generated in %.0fms (%d chars) model=%s key=...%s",
                         elapsed, len(text), model, key[-4:])
                return text
            log.warning("Gemini: empty response from model=%s key=...%s", model, key[-4:])
        except Exception as exc:
            log.warning("Gemini exception model=%s key=...%s: %s", model, key[-4:], exc)
            continue

    log.error("Gemini: all key×model combinations exhausted")
    return ""


@router.post("")
async def generate_report(request: Request):
    t0 = time.perf_counter()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"report": "Invalid request body.", "charts": [], "keyStats": [], "summary": "", "title": ""})

    question: str = body.get("question", "")
    sources: list[dict] = body.get("sources", [])

    log.info("Report request: question=%r  sources=%d", question[:80], len(sources))

    # If no sources were passed, run our own Tavily search so the report always has real data
    if not sources:
        log.info("Report: no sources from client — running own Tavily search for %r", question[:60])
        from routes.chat import tavily_search as _tavily_search, _looks_like_ai_overview
        searched = await _tavily_search(question, max_results=25)
        sources = [
            {"title": r["title"], "url": r["url"],
             "snippet": r["snippet"], "fullContent": r.get("fullContent", "")}
            for r in searched
        ]
        log.info("Report: self-search returned %d sources", len(sources))
    else:
        from routes.chat import _looks_like_ai_overview

    if not sources:
        log.warning("Report: still no sources after self-search")
        return JSONResponse({"report": "Could not retrieve data for this topic. Please try again.", "charts": [], "keyStats": [], "summary": "", "title": ""})

    async def enrich(src: dict, idx: int) -> dict:
        if _looks_like_ai_overview(src.get("snippet", "")):
            src = {**src, "snippet": ""}
        if _looks_like_ai_overview(src.get("fullContent", "")):
            src = {**src, "fullContent": ""}
        if len(src.get("fullContent", "")) > 600:
            return src
        if idx < 15:
            fetched = await fetch_page_content(src["url"], 1500)
            if _looks_like_ai_overview(fetched):
                return src
            if len(fetched) > len(src.get("snippet", "")):
                log.debug("Enriched source %d: %s (+%d chars)", idx + 1, src.get("title", "")[:40], len(fetched))
                return {**src, "fullContent": fetched}
        return src

    log.info("Report: enriching up to 25 sources...")
    enriched = list(await asyncio.gather(*[enrich(s, i) for i, s in enumerate(sources[:12])]) )
    log.info("Report: enrichment done (%d sources ready)", len(enriched))

    src_text = "\n\n---\n\n".join(
        f"[{i+1}] **{s['title']}**\nSource: {s['url']}\n"
        + (s["fullContent"][:1500] if len(s.get("fullContent", "")) > len(s.get("snippet", "")) else s.get("snippet", "")[:800])
        for i, s in enumerate(enriched)
    )

    user_prompt = (
        f"Research Question / Topic: {question}\n\n"
        f"Scraped content from the top {len(enriched)} web sources:\n\n{src_text}\n\n"
        "INSTRUCTIONS:\n"
        "1. Extract ALL numbers, tables, percentages, and statistics verbatim from the sources above.\n"
        "2. Follow the CHART RULES in the system prompt exactly — only produce charts for real distinct data.\n"
        "3. Write the full 6-section report. Insert [CHART_n] placeholders inline only where valid chart data exists.\n"
        "4. Respond ONLY with the JSON object — no markdown fences, no text outside JSON."
    )

    # Hard cap: total prompt must not exceed 12K chars regardless of source count
    MAX_PROMPT = 12_000
    if len(user_prompt) > MAX_PROMPT:
        # Trim src_text portion only, keep header + instructions intact
        header_end = user_prompt.find("\n\nINSTRUCTIONS")
        if header_end == -1:
            user_prompt = user_prompt[:MAX_PROMPT]
        else:
            budget = MAX_PROMPT - (len(user_prompt) - header_end)
            user_prompt = user_prompt[:budget] + "\n[sources trimmed]" + user_prompt[header_end:]
        log.info("Report: prompt hard-capped to %d chars", len(user_prompt))

    raw = await call_groq(user_prompt)
    if not raw:
        log.info("Report: Groq failed — trying Gemini fallback")
        raw = await call_gemini(user_prompt)
    if not raw:
        log.error("Report: all LLM providers exhausted")
        return JSONResponse({
            "report": "All LLM keys exhausted or rate-limited. Try again in a minute.",
            "charts": [], "keyStats": [], "summary": "", "title": "",
        })

    # Strip markdown fences (Gemini without responseMimeType may add them)
    clean = raw.strip()
    clean = re.sub(r"^```json\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^```\s*", "", clean)
    clean = re.sub(r"```\s*$", "", clean).strip()
    # If response starts with text before the JSON object, find the first {
    brace_idx = clean.find("{")
    if brace_idx > 0:
        log.debug("Report: skipping %d chars of preamble before JSON", brace_idx)
        clean = clean[brace_idx:]

    try:
        parsed = json.loads(clean)

        def _is_plausible_chart(ch: dict) -> bool:
            """Reject charts that look invented rather than sourced from real data."""
            series = ch.get("series") or []
            if not series or not ch.get("type") or not ch.get("title"):
                return False
            all_pts = [pt for s in series for pt in (s.get("data") or [])]
            if len(all_pts) < 2:
                return False
            values = [pt.get("value", 0) for pt in all_pts]
            labels = [str(pt.get("label", "")) for pt in all_pts]
            # Reject duplicate labels
            if len(set(labels)) < len(labels):
                log.warning("Chart rejected — duplicate labels: %s", labels[:6])
                return False
            # Reject all-identical values
            if len(set(values)) <= 1:
                log.warning("Chart rejected — identical values: %s", values[:6])
                return False
            # Reject line charts with values that look like evenly-spaced fabricated price levels
            # (e.g. [36121, 48915, 61709, 74503] — suspiciously arithmetic progression)
            if ch.get("type") == "line" and len(values) >= 3:
                diffs = [abs(values[i+1] - values[i]) for i in range(len(values)-1)]
                if diffs and max(diffs) > 0:
                    variance = sum((d - sum(diffs)/len(diffs))**2 for d in diffs) / len(diffs)
                    cv = (variance ** 0.5) / (sum(diffs)/len(diffs))
                    if cv < 0.05:  # coefficient of variation <5% = suspiciously even spacing
                        log.warning("Chart rejected — values look arithmetically generated (cv=%.3f): %s", cv, values)
                        return False
            return True

        charts = [c for c in (parsed.get("charts") or []) if _is_plausible_chart(c)]
        if len(charts) < len(parsed.get("charts") or []):
            log.info("Chart validation: kept %d / %d charts", len(charts), len(parsed.get("charts") or []))
        elapsed = (time.perf_counter() - t0) * 1000
        log.info(
            "Report complete in %.0fms — title=%r  charts=%d  keyStats=%d",
            elapsed, parsed.get("title", "")[:60], len(charts), len(parsed.get("keyStats", [])),
        )
        report_text = parsed.get("report", "")

        # Safety pass 1: unescape literal \n that some models emit
        if "\\n" in report_text:
            report_text = report_text.replace("\\n", "\n")

        # Safety pass 2: strip markdown/json fences
        report_text = re.sub(r"^```(?:json|markdown)?\s*", "", report_text.strip())
        report_text = re.sub(r"```\s*$", "", report_text).strip()

        # Safety pass 3: if the model stuffed the ENTIRE JSON response into the report field,
        # unwrap it. This happens when Gemini/Groq returns JSON inside the "report" string.
        for _attempt in range(2):
            stripped = report_text.strip()
            if not stripped.startswith("{"):
                break
            try:
                inner = json.loads(stripped)
                if not isinstance(inner, dict) or "report" not in inner:
                    break
                inner_report = (inner.get("report") or "").replace("\\n", "\n").strip()
                # Pull nested fields back out if they weren't already set
                if not charts and inner.get("charts"):
                    charts = [c for c in (inner["charts"] or [])
                              if c.get("type") and c.get("series")]
                for key in ("keyStats", "summary", "title"):
                    if not parsed.get(key) and inner.get(key):
                        parsed[key] = inner[key]
                report_text = inner_report
                log.warning("Report: unwrapped double-encoded JSON from report field (pass %d)", _attempt + 1)
            except Exception:
                break

        # Safety pass 4: if the report still starts with { and contains "title" + "report"
        # keys it's still raw JSON — extract just the report field one final time
        if report_text.strip().startswith("{") and '"report"' in report_text:
            m = re.search(r'"report"\s*:\s*"((?:[^"\\]|\\.)*)"', report_text)
            if m:
                report_text = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                log.warning("Report: regex-extracted report field from raw JSON (pass 4)")

        return JSONResponse({
            "title":    parsed.get("title", question[:80]),
            "report":   report_text,
            "charts":   charts,
            "keyStats": parsed.get("keyStats", []),
            "summary":  parsed.get("summary", ""),
        })
    except Exception as exc:
        log.error("Report: JSON parse failed: %s  (raw length: %d)", exc, len(raw))

        def _try_extract_fields(text: str) -> dict | None:
            """Best-effort field extraction from truncated/malformed JSON."""
            result = {}

            # 1. Try extracting "title" with a regex
            m = re.search(r'"title"\s*:\s*"([^"\\\n]{1,200})"', text)
            if m:
                result["title"] = m.group(1)

            # 2. Try extracting "summary"
            m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if m:
                result["summary"] = m.group(1).replace("\\n", "\n")

            # 3. Try extracting "report" — handle truncation by taking everything up to last complete section
            m = re.search(r'"report"\s*:\s*"((?:[^"\\]|\\.)*)', text)
            if m:
                report_raw = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                # Trim to last complete markdown heading or paragraph
                for boundary in ["\n## ", "\n### ", "\n\n"]:
                    last = report_raw.rfind(boundary)
                    if last > len(report_raw) * 0.4:
                        report_raw = report_raw[:last].strip()
                        break
                if len(report_raw) > 200:
                    result["report"] = report_raw + "\n\n*Note: Report was truncated due to response length limits.*"

            # 4. Try extracting keyStats array
            m = re.search(r'"keyStats"\s*:\s*(\[[\s\S]*?\])', text)
            if m:
                try:
                    result["keyStats"] = json.loads(m.group(1))
                except Exception:
                    pass

            # 5. Try extracting charts array — best effort, may be partial
            m = re.search(r'"charts"\s*:\s*(\[[\s\S]*?\]\s*[,}])', text)
            if m:
                try:
                    arr_text = m.group(1).rstrip(",}").strip()
                    result["charts"] = json.loads(arr_text)
                except Exception:
                    pass

            return result if result.get("report") else None

        # Try 1: regex-based field extraction from truncated output
        salvaged = _try_extract_fields(clean)
        if salvaged:
            log.info("Report: salvaged %d fields from truncated JSON (report=%d chars)",
                     len(salvaged), len(salvaged.get("report", "")))
            return JSONResponse({
                "title":    salvaged.get("title", question[:80]),
                "report":   salvaged.get("report", ""),
                "charts":   salvaged.get("charts", []),
                "keyStats": salvaged.get("keyStats", []),
                "summary":  salvaged.get("summary", ""),
            })

        # Try 2: attempt json.loads on a repaired string (close open strings/arrays)
        try:
            repaired = clean
            # Close any unterminated string by finding last quote boundary
            open_strings = repaired.count('"') % 2
            if open_strings:
                repaired = repaired + '"'
            # Close any unclosed arrays/objects
            opens = repaired.count("{") - repaired.count("}")
            repaired = repaired + ("}" * max(opens, 0))
            opens_arr = repaired.count("[") - repaired.count("]")
            repaired = repaired + ("]" * max(opens_arr, 0))
            repaired_parsed = json.loads(repaired)
            log.info("Report: repaired truncated JSON successfully")
            return JSONResponse({
                "title":    repaired_parsed.get("title", question[:80]),
                "report":   (repaired_parsed.get("report", "") or "").replace("\\n", "\n"),
                "charts":   repaired_parsed.get("charts", []),
                "keyStats": repaired_parsed.get("keyStats", []),
                "summary":  repaired_parsed.get("summary", ""),
            })
        except Exception:
            pass

        # Total failure
        log.error("Report: could not salvage JSON — returning error message")
        return JSONResponse({
            "title": question[:80],
            "report": "## Report Generation Error\n\nThe AI response was too long and could not be parsed. Please try a more specific question.",
            "charts": [], "keyStats": [], "summary": "",
        })
