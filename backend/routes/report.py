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

from utils.rag_client import rag_report as _rag_report

router = APIRouter()
log = logging.getLogger("report")

SKIP_PAGE_FETCH = [".pdf", "bloomberg.com", "wsj.com", "ft.com", "economist.com", "investing.com"]

SYSTEM_PROMPT = """You are a senior research analyst at Growth Gradual. Write a COMPREHENSIVE, well-structured research report on ANY topic using the scraped web sources provided.
You MUST respond with valid JSON only — no markdown fences, no preamble, no text outside JSON.

Respond with EXACTLY this shape:
{
  "title": "<concise report title, max 12 words — a noun-phrase headline like 'Global Growth Slowdown and AI Investment Trends', NEVER a sentence starting with 'Here are', 'Based on', 'The following', or similar preamble/instruction phrasing>",
  "report": "<full markdown report — target 1000-1400 words, structured and data-rich>",
  "charts": [...],
  "keyStats": [{ "label": "<short label>", "value": "<value string>", "change": "<+/- % or empty string>" }],
  "summary": "<2-3 sentence executive summary>"
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL DATA INTEGRITY RULES — VIOLATIONS DEGRADE REPORT QUALITY:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✗ NEVER invent, estimate, or hallucinate numbers. Only use figures explicitly present in the provided sources.
✗ NEVER use your training-data knowledge for specific numbers (index levels, stock prices, rates, percentages).
   Your training data is STALE — e.g. Sensex, Nifty, gold prices, FII flows MUST come from the sources provided.
✗ If a specific number is NOT in the sources, write "data not available from sources" rather than guessing.
✓ You MAY use your knowledge for definitions, context, explanations, and general market dynamics.
✓ Every key metric in keyStats and every chart data point must be traceable to the scraped source content.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHART RULES — READ CAREFULLY. CHARTS ARE MANDATORY WHERE DATA EXISTS.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — AGGRESSIVELY SCAN sources for ANY chartable numbers:
  • Returns/performance of multiple funds, stocks, sectors → bar chart
  • Rankings with numbers (top 5 SIPs by return, top gainers) → bar chart
  • Time-series: quarterly results, monthly data, weekly prices → line chart
  • Allocation/composition (sector weights, portfolio mix) → pie chart
  • Comparisons: 1yr vs 3yr vs 5yr returns of same fund → bar chart
  • FII/DII flows by date → line or bar chart
  • Category-wise data (large cap vs mid cap vs small cap) → bar chart

  COMPARISON TOPICS (X vs Y, A vs B) — MANDATORY MULTI-SERIES CHART:
  When the question compares TWO OR MORE assets (e.g. "Gold vs Silver",
  "Nifty vs Sensex", "HDFC vs ICICI", "equity vs debt"):
  → Create ONE line chart with BOTH items as SEPARATE SERIES sharing the SAME date labels
  → Example: series: [{"name":"Gold","data":[{"label":"Jun 1","value":1820},{"label":"Jun 5","value":1810}]},
                       {"name":"Silver","data":[{"label":"Jun 1","value":86},{"label":"Jun 5","value":81}]}]
  → NEVER make two separate single-series charts for each item — always combine into one
  → If price scales differ wildly (e.g. gold Rs.1,25,000 vs silver Rs.72,000 when % change is small),
     use % change from start for ALL series so both fit on the same Y axis:
     → {"label":"Jun 1","value":0.0} for both, then show % change from that base
  → NEVER mix price values and percentage changes in the same chart series or bar group.
     BAD: series=[{name:"Gold",data:[{label:"Current Price",value:125957},{label:"Change",value:-3}]}, ...]
     GOOD: Make ONE chart for price trend (line, % change), ONE separate bar chart for key stats like 1-month return %
  → Also add a bar chart comparing key stats (1-month return %, 52-week high/low, current price) — but ONLY if you have ≥3 distinct comparable stats from the sources

  For EVERY topic, these charts almost ALWAYS make sense — create them if data exists:
  • SIP topic → bar chart of top 5 funds by 3-yr return %
  • Stock topic → bar chart of key financial metrics (revenue, profit growth %)
  • Banking topic → bar chart of NIM/NPA/ROE across banks
  • Market topic → bar chart of sector performance %
  • Mutual fund topic → pie chart of category allocation OR bar of returns by category

STEP 2 — ONLY create a chart if ALL conditions are met:
  ✓ At least 3 data points (bar/pie) or 4 time points (line)
  ✓ All labels are DIFFERENT from each other
  ✓ All values are DIFFERENT from each other (not all the same)
  ✓ Values come from the source data — do NOT invent numbers
  ✗ NEVER create a chart from a single number
  ✗ NEVER duplicate labels
  ✗ NEVER use future/projected values you invented
  ✗ A bar/pie needs ≥3 named distinct items

STEP 3 — Place [CHART_n] inline in the report markdown right after the paragraph whose data it shows.
  charts[0] = [CHART_1], charts[1] = [CHART_2], etc.

STEP 4 — Aim for 2-4 charts per report when data supports it. Quality over quantity.
  If sources genuinely lack numeric data → 0 charts is acceptable. But look hard first.

Chart spec shape:
{
  "type": "bar" | "line" | "pie",
  "title": "<specific title e.g. 'Top 5 SIP Funds — 3-Year Returns' not 'Chart 1'>",
  "unit": "%" | "₹" | "Cr" | "B" | "$" | "x" | "",
  "series": [{ "name": "<series name>", "data": [{ "label": "<unique label>", "value": <number> }] }]
}

GOOD chart examples — do exactly this:
  • "Top SIP Funds by 3-Yr Return" → bar, labels=[ICICI Pru Value, Nippon India Value, UTI Gold ETF, Quant Small Cap], values=[15.9, 15.8, 35.2, 28.4], unit="%"
  • "Sectoral PAT Growth Q4FY26" → bar, labels=[Utilities, Metals, Retail, Healthcare, BFSI], values=[61,53,32,32,18], unit="%"  
  • "Nifty 50 Quarterly EPS" → line, labels=[Q1FY25,Q2FY25,Q3FY25,Q4FY25,Q1FY26], values=[actual numbers from source]
  • "Top Banking Stocks — ROE %" → bar, labels=[HDFC Bank, ICICI Bank, Kotak, SBI, Axis], values from source
  • "MF Category Inflows" → pie, labels=[Large Cap, Mid Cap, Small Cap, Flexi Cap, ELSS], values=₹ crore

BAD chart examples — NEVER do this:
  ✗ labels=["Today","Today","Today"] — duplicate labels
  ✗ values=[100, 100, 100] — identical values  
  ✗ Inventing numbers not in sources
  ✗ Line chart with only 2 data points
  ✗ [CHART_n] in report without matching charts[n-1] entry

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPORT STRUCTURE (each section MUST be substantive — minimum 150 words per section):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# [Report Title]

## 1. Introduction
3-4 paragraphs (minimum 150 words): Provide rich context — explain the sector/topic, why it matters now, who the key stakeholders are, what macro or market forces are driving interest, and what this report covers. Cite [n]. No filler, every sentence must add context or data.

## 2. Data Sources & Methodology
One markdown table of sources (Publication | URL | Data type). 2 short paragraphs: describe what sources were used, what metrics were collected, how comparisons were made, and any caveats in the data.

## 3. Data Analysis

### 3.1 [Heading matching actual topic and data]
Deep-dive quantitative findings (minimum 120 words). ALL numbers in markdown tables. Explain what the data shows, why the leaders are ahead, what the numbers mean in context. Insert [CHART_n] immediately after the paragraph whose data it visualises.

### 3.2 [Second dimension of analysis]
Comparisons, breakdowns, benchmarks (minimum 120 words). Tables + [CHART_n] where valid distinct data exists. Explain trends and what differentiates top performers.

### 3.3 [Trends or forward-looking data — only if sources have it]
Only include if sources have trend/time-series data with ≥4 distinct time points. Minimum 100 words if included.

## 4. Key Findings
8-10 numbered findings, each with a specific number/stat from sources AND a 1-2 sentence explanation of what it means. Cite [n] on every finding.

## 5. Conclusion
2-3 paragraphs (minimum 120 words): synthesise findings, explain implications for different types of investors/stakeholders, and give forward outlook from sources only.

## 6. References
[n] Publication. Title. URL (date if known)

GLOBAL RULES:
- NEVER invent any number, date, name, or statistic
- TABLE HYGIENE: every column must have real values for EVERY row. If a column would
  be "-" or empty for some rows (e.g. indices like NIFTY 50/NIFTY BANK have no market cap),
  do NOT include that column for those rows — instead, put indices and individual stocks in
  SEPARATE tables (one for index levels, one for stock market caps), or drop the column entirely
  if it doesn't apply to the row type being shown.
- Cite [n] throughout every section
- At least 3 markdown data tables
- keyStats: 6-8 real metrics with values and change indicators
- Target 1000-1400 words total — every section must be substantive; never stub a section with a single sentence
- CRITICAL: NEVER write raw JSON inside the "report" string. Charts go ONLY in the "charts" array.
  Use [CHART_1], [CHART_2] placeholders in the report text — never paste the chart JSON object itself.
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


def _extract_inline_chart_jsons(report_text: str, existing_charts: list) -> tuple[str, list]:
    """Find raw {"type": ..., "series": ...} chart objects the LLM leaked
    inline into the report body, replace each with a [CHART_n] placeholder
    (appending to existing_charts so it actually renders), and return the
    cleaned text + updated charts list.

    Uses brace-matched scanning instead of a single regex so nested objects
    (e.g. each data point's own {"label": ..., "value": ...}) don't cause the
    match to terminate early on the first inner "}".
    """
    charts = list(existing_charts)
    out = []
    i = 0
    n = len(report_text)
    while i < n:
        ch = report_text[i]
        if ch == "{":
            # Find the matching closing brace via depth counting, honoring
            # quoted strings so braces inside string values don't confuse it.
            depth = 0
            j = i
            in_str = False
            esc = False
            while j < n:
                c = report_text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1

            candidate = report_text[i:j]
            if depth == 0 and '"type"' in candidate and '"series"' in candidate:
                try:
                    spec = json.loads(candidate)
                except Exception:
                    spec = None
                if isinstance(spec, dict) and spec.get("series") is not None:
                    charts.append(spec)
                    out.append(f"[CHART_{len(charts)}]")
                    i = j
                    continue
            # Not a chart object (or invalid JSON) — emit the opening brace
            # as-is and continue scanning from the next character.
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1

    cleaned = "".join(out)
    # Collapse any blank-line runs left behind by removed blobs
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, charts


# Groq context limit: ~6K tokens input. Trim prompt to avoid 413.
GROQ_MAX_PROMPT_CHARS = 40_000  # Groq supports large contexts — only trim if truly enormous

def _trim_for_groq(prompt: str) -> str:
    """Only trim if prompt is genuinely huge (>40K chars)."""
    if len(prompt) <= GROQ_MAX_PROMPT_CHARS:
        return prompt
    # Keep header + as much source content as fits
    header_end = prompt.find("Scraped content")
    if header_end == -1:
        return prompt[:GROQ_MAX_PROMPT_CHARS]
    header = prompt[:header_end + 100]
    body   = prompt[header_end + 100:]
    allowed = GROQ_MAX_PROMPT_CHARS - len(header) - 200
    trimmed_body = body[:allowed]
    last_sep = trimmed_body.rfind("\n---\n")
    if last_sep > allowed * 0.5:
        trimmed_body = trimmed_body[:last_sep]
    footer = "\n\nINSTRUCTIONS:\n1. Extract ALL numbers, tables, percentages verbatim.\n2. Follow CHART RULES exactly.\n3. Respond ONLY with the JSON object."
    result = header + trimmed_body + footer
    log.info("Groq: trimmed prompt %d→%d chars", len(prompt), len(result))
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
                        "max_tokens": 16000,
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
                                "max_tokens": 16000,
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
                # 413 means the payload is too large for this key's context window.
                # It says nothing about other keys, so only mark this one key
                # as unavailable briefly and let the loop try the next key.
                # (Blanket-marking every key poisons the shared chat endpoint too.)
                mark_rate_limited(key, 120_000)
                continue
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
    # Ordered by RPD on the free tier so the highest-quota model is tried first.
    # gemini-3.1-flash-lite: 15 RPM / 500 RPD  ← workhorse; try this first
    # gemini-2.5-flash-lite: 10 RPM / 20 RPD
    # gemini-2.5-flash:       5 RPM / 20 RPD
    # gemini-3-flash-preview: 5 RPM / 20 RPD   (API string; shown as "Gemini 3 Flash" in console)
    # gemini-3.5-flash:       5 RPM / 20 RPD
    # Removed: gemini-2.0-flash / gemini-2.0-flash-lite (retiring June 2026, 0/0/0 quota)
    "gemini-3.1-flash-lite",   # 15 RPM / 500 RPD — highest free quota by far
    "gemini-2.5-flash-lite",   # 10 RPM / 20 RPD
    "gemini-2.5-flash",        #  5 RPM / 20 RPD
    "gemini-3-flash-preview",  #  5 RPM / 20 RPD
    "gemini-3.5-flash",        #  5 RPM / 20 RPD
]


async def call_gemini(user_prompt: str) -> str:
    keys = get_gemini_keys()
    if not keys:
        log.warning("Gemini: no keys configured for report generation")
        return ""

    # Filter to AIzaSy* keys only — gen-lang-client-* are Vertex AI keys that
    # return 400 Bad Request on generativelanguage.googleapis.com REST API.
    rest_keys = [k for k in keys if k.startswith("AIzaSy")]
    if not rest_keys:
        log.warning("Gemini: no AIzaSy* keys available — all keys are gen-lang-client type")
        return ""

    log.info("Gemini: attempting report generation with %d key(s), %d models", len(rest_keys), len(GEMINI_MODELS))

    # Build a flat attempt list: (key, model) — cycle through all key×model combos
    # Priority: try each model with the least-used key first, then next model, etc.
    available_keys = [k for k in round_robin(rest_keys) if not is_rate_limited(k)]
    if not available_keys:
        available_keys = rest_keys  # all rate-limited — try anyway as last resort

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
            generation_config = {
                "maxOutputTokens": 32000,
                "temperature": 0.1,
                "responseMimeType": "application/json",  # force JSON — avoids markdown fences
            }
            # Suppress thinking tokens for structured-output tasks — we want the
            # full token budget to go to the actual JSON report, not hidden reasoning.
            # Gemini 2.5 uses the legacy thinkingBudget:0 param.
            # Gemini 3 uses thinkingLevel (mixing both in one request → 400).
            # gemini-3.1-flash-lite defaults to "minimal" so no param needed.
            if model in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
                generation_config["thinkingConfig"] = {"thinkingBudget": 0}
            elif model in ("gemini-3-flash-preview", "gemini-3.5-flash"):
                generation_config["thinkingConfig"] = {"thinkingLevel": "minimal"}
            async with httpx.AsyncClient(timeout=120) as client:
                res = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    json={
                        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                        "generationConfig": generation_config,
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


async def extract_data_from_images(question: str, file_images: list[dict]) -> str:
    """
    Use Gemini Vision to extract all text, tables, charts, and data from
    uploaded file page images. Returns a rich text context string.
    """
    keys = get_gemini_keys()
    if not keys:
        log.warning("extract_data_from_images: no Gemini keys")
        return ""

    # Build the Gemini multimodal request
    parts = [
        {
            "text": (
                f"You are analyzing pages from an uploaded research document. "
                f"The user's question is: \"{question}\"\n\n"
                "For EACH page image provided, extract EVERYTHING you can see:\n"
                "1. All text content, headings, paragraphs\n"
                "2. All tables — reproduce them exactly with their numbers\n"
                "3. All charts/graphs — describe the data they show, list all data points with labels and values\n"
                "4. All key statistics, percentages, figures\n"
                "5. Note which page each item came from (Page 1, Page 2, etc.)\n\n"
                "Format output as structured text with clear sections per page. "
                "Be exhaustive — every number matters for the report."
            )
        }
    ]

    # Add images (max 8 pages to stay within Gemini limits)
    for i, img in enumerate(file_images[:8]):
        parts.append({
            "inline_data": {
                "mime_type": img.get("mimeType", "image/jpeg"),
                "data": img["data"]
            }
        })
        parts.append({"text": f"[Above is page {i+1}: {img.get('name', f'page {i+1}')}]"})

    # Filter out gen-lang-client-* keys — these are Vertex AI / Google Cloud keys
    # that return 400 Bad Request on the generativelanguage.googleapis.com REST endpoint.
    # Only AIzaSy* keys work with the standard REST API used here.
    vision_keys = [k for k in keys if k.startswith("AIzaSy")]
    if not vision_keys:
        log.warning("extract_data_from_images: no AIzaSy* Gemini keys available for vision")
        return ""

    available_keys = [k for k in vision_keys if not is_rate_limited(k)]
    if not available_keys:
        available_keys = vision_keys

    for key in available_keys[:3]:  # try up to 3 keys
        for model in ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash"]:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    res = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                        json={
                            "contents": [{"role": "user", "parts": parts}],
                            "generationConfig": {"maxOutputTokens": 65536, "temperature": 0.1},
                        },
                    )
                if res.status_code == 429:
                    mark_rate_limited(key, 60_000)
                    break
                if res.status_code == 503:
                    continue
                if not res.is_success:
                    continue
                text = res.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                if text:
                    log.info("extract_data_from_images: extracted %d chars via %s", len(text), model)
                    return text
            except Exception as exc:
                log.warning("extract_data_from_images error model=%s: %s", model, exc)
                continue

    log.warning("extract_data_from_images: all attempts failed")
    return ""


@router.post("")
async def generate_report(request: Request):
    t0 = time.perf_counter()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"report": "Invalid request body.", "charts": [], "keyStats": [], "summary": "", "title": ""})

    question: str       = body.get("question", "")
    sources: list[dict] = body.get("sources", [])
    file_context: str   = body.get("fileContext", "")
    file_images: list[dict] = body.get("fileImages", [])
    session_id: str     = body.get("sessionId", "").strip()
    has_rag: bool       = bool(body.get("hasRag", False))

    log.info("Report request: question=%r  sources=%d  fileImages=%d  fileContext=%d chars  rag=%s",
             question[:80], len(sources), len(file_images), len(file_context), has_rag)

    # ── RAG-grounded report: if files were indexed, use RAG full-coverage retrieval ──
    if has_rag and session_id:
        log.info("Report: RAG mode — full-coverage retrieval for session %s", session_id[:8])
        rag_result = await _rag_report(
            session_id=session_id,
            report_spec=question,
            report_type="comprehensive",
        )
        if rag_result.get("has_content") and rag_result.get("system_prompt"):
            # Build a rich user prompt that includes the RAG grounded system prompt
            rag_file_context = rag_result["system_prompt"]
            log.info("Report: RAG retrieved %d chunks from %s",
                     rag_result.get("retrieved", 0), rag_result.get("source_files", []))
            # Inject RAG context into file_context so the existing pipeline uses it
            file_context = rag_file_context
            has_rag = False  # prevent double-calling

    # ── If file images were uploaded, use Gemini Vision to extract data ───────
    extracted_image_context = ""
    if file_images:
        log.info("Report: extracting data from %d file images via Gemini Vision", len(file_images))
        extracted_image_context = await extract_data_from_images(question, file_images)
        if extracted_image_context:
            log.info("Report: extracted %d chars from images", len(extracted_image_context))

    # ── Decide source strategy ─────────────────────────────────────────────────
    has_file_data = bool(file_context.strip()) or bool(extracted_image_context.strip())

    if has_file_data and not sources:
        from routes.chat import tavily_search as _tavily_search, _looks_like_ai_overview, needs_web_search as _needs_web_search
        if _needs_web_search(question, has_files=True):
            # Question implies it wants more than just the file (e.g. asks for
            # market context, comparisons, recent news) — supplement with web data.
            log.info("Report: file-first mode — supplementing with web search")
            searched = await _tavily_search(question, max_results=20, min_results=10)
            sources = [
                {"title": r["title"], "url": r["url"],
                 "snippet": r["snippet"], "fullContent": r.get("fullContent", "")}
                for r in searched
            ]
        else:
            # Generic "what is this / describe / summarise" type question about
            # an attached file/image — a web search on the literal question text
            # would just return irrelevant noise, so skip it entirely.
            log.info("Report: file-first mode — question doesn't need web search, using file data only")
    elif not sources:
        log.info("Report: no sources — running own Tavily search for %r", question[:60])
        from routes.chat import tavily_search as _tavily_search, _looks_like_ai_overview
        searched = await _tavily_search(question, max_results=20)
        sources = [
            {"title": r["title"], "url": r["url"],
             "snippet": r["snippet"], "fullContent": r.get("fullContent", "")}
            for r in searched
        ]
        log.info("Report: self-search returned %d sources", len(sources))
    else:
        from routes.chat import _looks_like_ai_overview

    if not sources and not has_file_data:
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

    log.info("Report: enriching sources...")
    enriched = list(await asyncio.gather(*[enrich(s, i) for i, s in enumerate(sources[:12])]))
    log.info("Report: enrichment done (%d sources ready)", len(enriched))

    src_text = "\n\n---\n\n".join(
        f"[{i+1}] **{s['title']}**\nSource: {s['url']}\n"
        + (s["fullContent"][:1500] if len(s.get("fullContent", "")) > len(s.get("snippet", "")) else s.get("snippet", "")[:800])
        for i, s in enumerate(enriched)
    )

    # ── Build user prompt — file content takes priority ───────────────────────
    file_section = ""
    if extracted_image_context:
        file_section += f"\n\n━━ DATA EXTRACTED FROM UPLOADED FILE IMAGES ━━\n{extracted_image_context[:8000]}\n━━ END FILE IMAGE DATA ━━\n"
    if file_context.strip():
        file_section += f"\n\n━━ UPLOADED FILE TEXT CONTENT ━━\n{file_context[:6000]}\n━━ END FILE CONTENT ━━\n"

    # Image placement instruction — tell LLM where to place page image references
    img_placement_instruction = ""
    if file_images:
        page_list = ", ".join(f"[PAGE_IMG_{i+1}] = \"{img['name']}\"" for i, img in enumerate(file_images[:8]))
        img_placement_instruction = (
            f"\n\nIMAGE PLACEMENT RULES:\n"
            f"The following page images from the uploaded file are available: {page_list}\n"
            f"When discussing data, charts, or tables that appear on a specific page, insert [PAGE_IMG_n] "
            f"on its own line right after the relevant paragraph. The PDF renderer will embed the actual page image there. "
            f"Only place [PAGE_IMG_n] where it genuinely adds context — do NOT place all images, only the most relevant ones (max 4)."
        )

    user_prompt = (
        f"Research Question / Topic: {question}\n"
        + (f"\nPRIMARY SOURCE — ANALYSE THIS FIRST (uploaded file data takes highest priority):{file_section}" if file_section else "")
        + (img_placement_instruction if img_placement_instruction else "")
        + f"\n\nSupplementary web sources ({len(enriched)} results):\n\n{src_text}\n\n"
        "INSTRUCTIONS:\n"
        "1. The uploaded file content (if provided) is your PRIMARY source — extract ALL numbers, tables, charts, and statistics from it first.\n"
        "2. Use web sources to supplement and validate the file data.\n"
        "3. Follow CHART RULES exactly — reproduce actual data from the file as charts where it exists.\n"
        "4. Write the full 6-section report. Insert [CHART_n] inline where valid chart data exists.\n"
        + ("5. Insert [PAGE_IMG_n] references inline where you reference data visible in that page image.\n" if file_images else "")
        + "6. Respond ONLY with the JSON object — no markdown fences, no text outside JSON."
    )

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

    # Strip markdown fences
    clean = raw.strip()
    clean = re.sub(r"^```json\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^```\s*", "", clean)
    clean = re.sub(r"```\s*$", "", clean).strip()
    brace_idx = clean.find("{")
    if brace_idx > 0:
        log.debug("Report: skipping %d chars of preamble before JSON", brace_idx)
        clean = clean[brace_idx:]

    try:
        parsed = json.loads(clean)

        def _is_plausible_chart(ch: dict) -> bool:
            series = ch.get("series") or []
            if not series or not ch.get("type") or not ch.get("title"):
                return False
            n_series = len(series)
            chart_type = ch.get("type", "bar")

            # For multi-series (comparison charts): validate each series individually
            for s in series:
                pts = s.get("data") or []
                # Multi-series line charts only need 2+ pts per series
                min_pts = 2 if chart_type == "line" else 1
                if len(pts) < min_pts:
                    log.warning("Chart rejected — series '%s' has only %d points", s.get("name","?"), len(pts))
                    return False

            all_pts = [pt for s in series for pt in (s.get("data") or [])]
            values  = [pt.get("value", 0) for pt in all_pts]
            if len(values) > 1 and len(set(values)) <= 1:
                log.warning("Chart rejected — identical values: %s", values[:6])
                return False

            # Reject bar charts where a single series mixes wildly different scales
            # (e.g. price 125957 and % change -3 as two bars in the same series)
            if chart_type == "bar":
                for s in series:
                    pts_vals = [pt.get("value", 0) for pt in (s.get("data") or [])]
                    if len(pts_vals) >= 2:
                        pos_vals = [v for v in pts_vals if v > 0]
                        neg_vals = [v for v in pts_vals if v < 0]
                        if pos_vals and neg_vals:
                            max_pos = max(pos_vals)
                            max_neg = abs(min(neg_vals))
                            if max_pos > 0 and max_neg > 0 and max_pos / max_neg > 100:
                                log.warning("Chart rejected — mixed scale in '%s': pos=%.0f neg=%.0f",
                                            s.get("name", "?"), max_pos, min(neg_vals))
                                return False

            # Check labels unique WITHIN each series (not across series)
            for s in series:
                labels = [str(pt.get("label", "")) for pt in (s.get("data") or [])]
                if len(set(labels)) < len(labels):
                    log.warning("Chart rejected — duplicate labels in series '%s': %s", s.get("name","?"), labels[:6])
                    return False

            # For single-series line charts: reject if values look arithmetically generated
            if chart_type == "line" and n_series == 1 and len(values) >= 3:
                diffs = [abs(values[i+1] - values[i]) for i in range(len(values)-1)]
                if diffs and max(diffs) > 0:
                    variance = sum((d - sum(diffs)/len(diffs))**2 for d in diffs) / len(diffs)
                    cv = (variance ** 0.5) / (sum(diffs)/len(diffs))
                    if cv < 0.05:
                        log.warning("Chart rejected — values look arithmetically generated (cv=%.3f): %s", cv, values)
                        return False
            return True

        charts = [c for c in (parsed.get("charts") or []) if _is_plausible_chart(c)]
        if len(charts) < len(parsed.get("charts") or []):
            log.info("Chart validation: kept %d / %d charts", len(charts), len(parsed.get("charts") or []))

        elapsed = (time.perf_counter() - t0) * 1000
        log.info("Report complete in %.0fms — title=%r  charts=%d  keyStats=%d",
                 elapsed, parsed.get("title", "")[:60], len(charts), len(parsed.get("keyStats", [])))

        report_text = parsed.get("report", "")
        if "\\n" in report_text:
            report_text = report_text.replace("\\n", "\n")
        report_text = re.sub(r"^```(?:json|markdown)?\s*", "", report_text.strip())
        report_text = re.sub(r"```\s*$", "", report_text).strip()
        # Strip raw JSON chart blobs the LLM leaked into the report body and
        # convert each into a [CHART_n] placeholder so it still renders.
        report_text, charts = _extract_inline_chart_jsons(report_text, charts)

        for _attempt in range(2):
            stripped = report_text.strip()
            if not stripped.startswith("{"):
                break
            try:
                inner = json.loads(stripped)
                if not isinstance(inner, dict) or "report" not in inner:
                    break
                inner_report = (inner.get("report") or "").replace("\\n", "\n").strip()
                if not charts and inner.get("charts"):
                    charts = [c for c in (inner["charts"] or []) if c.get("type") and c.get("series")]
                for key in ("keyStats", "summary", "title"):
                    if not parsed.get(key) and inner.get(key):
                        parsed[key] = inner[key]
                report_text = inner_report
                log.warning("Report: unwrapped double-encoded JSON from report field (pass %d)", _attempt + 1)
            except Exception:
                break

        if report_text.strip().startswith("{") and '"report"' in report_text:
            m = re.search(r'"report"\s*:\s*"((?:[^"\\]|\\.)*)"', report_text)
            if m:
                report_text = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                log.warning("Report: regex-extracted report field from raw JSON (pass 4)")

        return JSONResponse({
            "title":      parsed.get("title", question[:80]),
            "report":     report_text,
            "charts":     charts,
            "keyStats":   parsed.get("keyStats", []),
            "summary":    parsed.get("summary", ""),
            "fileImages": file_images,  # pass back so PDF can embed them
        })
    except Exception as exc:
        log.error("Report: JSON parse failed: %s  (raw length: %d)", exc, len(raw))

        def _try_extract_fields(text: str) -> dict | None:
            result = {}
            m = re.search(r'"title"\s*:\s*"([^"\\\n]{1,200})"', text)
            if m: result["title"] = m.group(1)
            m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if m: result["summary"] = m.group(1).replace("\\n", "\n")
            m = re.search(r'"report"\s*:\s*"((?:[^"\\]|\\.)*)', text)
            if m:
                report_raw = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                for boundary in ["\n## ", "\n### ", "\n\n"]:
                    last = report_raw.rfind(boundary)
                    if last > len(report_raw) * 0.4:
                        report_raw = report_raw[:last].strip()
                        break
                if len(report_raw) > 200:
                    result["report"] = report_raw + "\n\n*Note: Report was truncated due to response length limits.*"
            m = re.search(r'"keyStats"\s*:\s*(\[[\s\S]*?\])', text)
            if m:
                try: result["keyStats"] = json.loads(m.group(1))
                except Exception: pass
            m = re.search(r'"charts"\s*:\s*(\[[\s\S]*?\]\s*[,}])', text)
            if m:
                try:
                    arr_text = m.group(1).rstrip(",}").strip()
                    result["charts"] = json.loads(arr_text)
                except Exception: pass
            return result if result.get("report") else None

        salvaged = _try_extract_fields(clean)
        if salvaged:
            log.info("Report: salvaged %d fields from truncated JSON", len(salvaged))
            return JSONResponse({
                "title":      salvaged.get("title", question[:80]),
                "report":     salvaged.get("report", ""),
                "charts":     salvaged.get("charts", []),
                "keyStats":   salvaged.get("keyStats", []),
                "summary":    salvaged.get("summary", ""),
                "fileImages": file_images,
            })

        try:
            repaired = clean
            if repaired.count('"') % 2: repaired += '"'
            opens = repaired.count("{") - repaired.count("}")
            repaired += "}" * max(opens, 0)
            opens_arr = repaired.count("[") - repaired.count("]")
            repaired += "]" * max(opens_arr, 0)
            repaired_parsed = json.loads(repaired)
            log.info("Report: repaired truncated JSON successfully")
            return JSONResponse({
                "title":      repaired_parsed.get("title", question[:80]),
                "report":     (repaired_parsed.get("report", "") or "").replace("\\n", "\n"),
                "charts":     repaired_parsed.get("charts", []),
                "keyStats":   repaired_parsed.get("keyStats", []),
                "summary":    repaired_parsed.get("summary", ""),
                "fileImages": file_images,
            })
        except Exception:
            pass

        log.error("Report: could not salvage JSON — returning error message")
        return JSONResponse({
            "title": question[:80],
            "report": "## Report Generation Error\n\nThe AI response could not be parsed. Please try a more specific question.",
            "charts": [], "keyStats": [], "summary": "", "fileImages": file_images,
        })

    question: str = body.get("question", "")
    sources: list[dict] = body.get("sources", [])

    log.info("Report request: question=%r  sources=%d", question[:80], len(sources))

    # If no sources were passed, run our own Tavily search so the report always has real data
    if not sources:
        log.info("Report: no sources from client — running own Tavily search for %r", question[:60])
        from routes.chat import tavily_search as _tavily_search, _looks_like_ai_overview
        searched = await _tavily_search(question, max_results=20)
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
            n_series   = len(series)
            chart_type = ch.get("type", "bar")
            for s in series:
                pts = s.get("data") or []
                min_pts = 2 if chart_type == "line" else 1
                if len(pts) < min_pts:
                    log.warning("Chart rejected — series '%s' has only %d points", s.get("name","?"), len(pts))
                    return False
            all_pts = [pt for s in series for pt in (s.get("data") or [])]
            values  = [pt.get("value", 0) for pt in all_pts]
            if len(values) > 1 and len(set(values)) <= 1:
                log.warning("Chart rejected — identical values: %s", values[:6])
                return False
            for s in series:
                labels = [str(pt.get("label", "")) for pt in (s.get("data") or [])]
                if len(set(labels)) < len(labels):
                    log.warning("Chart rejected — duplicate labels in '%s': %s", s.get("name","?"), labels[:6])
                    return False
            if chart_type == "line" and n_series == 1 and len(values) >= 3:
                diffs = [abs(values[i+1] - values[i]) for i in range(len(values)-1)]
                if diffs and max(diffs) > 0:
                    variance = sum((d - sum(diffs)/len(diffs))**2 for d in diffs) / len(diffs)
                    cv = (variance ** 0.5) / (sum(diffs)/len(diffs))
                    if cv < 0.05:
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

        # Safety pass 2b: convert any raw JSON chart blobs the LLM leaked into
        # the report body into [CHART_n] placeholders so they still render.
        report_text, charts = _extract_inline_chart_jsons(report_text, charts)

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
