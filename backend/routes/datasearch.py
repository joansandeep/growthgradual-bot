"""
POST /api/datasearch  — turn a free-text query into structured data points.

Body:  { "query": str }
Reply: {
  "query": str,
  "dataPoints": [
    { "entity": str, "metric": str, "value": number|str, "unit": str,
      "period": str, "sourceTitle": str, "sourceUrl": str, "kind": "live"|"web" }
  ],
  "sources": [{ "title": str, "url": str }],
  "sourceCount": int,
  "generatedAt": str (ISO)
}

This is the "search engine that returns data points" behind the dashboard /
Excel / CSV export flow — deliberately NOT the narrative report pipeline in
routes/report.py. Two data streams feed the result:
  1. Live, verified numeric fundamentals for any companies the query names
     (via utils.market_data — same source routes/report.py trusts most).
  2. Web search results (Tavily) run through JSON-mode LLM passes that pull
     out only numbers explicitly present in the source text — no
     fabrication, every row keeps its source.

── Reaching ~100 source URLs ──────────────────────────────────────────────
Tavily caps a single search call at 20 results, and routes.chat.tavily_search
already fans one query out across every configured TAVILY_API_KEY in
parallel (deduped by URL) — but same query + same ranking means that alone
still converges on ~20 unique URLs, not 100, regardless of key count.
To actually use however many keys are configured (this deployment: 5), we
expand the user's request into up to 5 differently-angled search queries
(via one quick LLM call) and run them through tavily_search_multi(), which
runs tavily_search() — full multi-key fan-out included — once per angle in
parallel and merges everything deduped by URL. Each angle can return up to
20 results, so 5 angles caps out at up to 100 unique source URLs.
"""
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from routes.chat import tavily_search_multi
from utils.keys import get_tavily_keys
from utils.llm_extract import extract_json
from utils.market_data import fetch_stock_fundamentals

router = APIRouter()
log = logging.getLogger("datasearch")

MAX_ANGLES = 5           # one per configured Tavily key, capped at 5
RESULTS_PER_ANGLE = 20   # Tavily's hard per-call max
BATCH_SIZE = 50          # sources per extraction LLM call — 50 * ~4000 chars
                          # of content is ~50K tokens, well inside Groq's
                          # ~128K primary context and a fraction of Gemini's
                          # 1M-token fallback context, so 100 sources now
                          # take 2 batches instead of 5 (fewer, richer calls)
MAX_DATA_POINTS = 150    # sanity cap on the final table size

_QUERY_EXPANSION_SYSTEM_PROMPT = """You expand a data request into several \
distinct web-search queries that together cover it thoroughly from different \
angles (e.g. official company filings/reports, financial news coverage, \
analyst/industry data, historical figures, competitor comparisons) — pick \
whichever angles actually fit the request rather than forcing all of them.

Respond with ONLY a JSON object of this exact shape, nothing else:
{ "queries": ["search query 1", "search query 2", ...] }
Return between 2 and 5 queries. Each must be a short, concrete search-engine \
query (not a sentence), under 400 characters. If the request is already \
narrow and specific, fewer, more targeted queries are better than padding \
to 5."""

_EXTRACTION_SYSTEM_PROMPT = """You are a data-extraction engine. You are given a user's data \
request and a set of web search results. Your only job is to pull out concrete, \
quantitative or clearly-stated data points that answer the request, using ONLY \
numbers and facts that literally appear in the provided source text. Never \
estimate, infer, or round a number that isn't stated. If a source doesn't \
contain a usable data point, skip it.

Also identify any specific company names mentioned in the user's request that \
would benefit from a live stock-fundamentals lookup (empty list if none / not \
applicable — e.g. skip this for macro, commodity, or non-company queries).

Respond with ONLY a JSON object of this exact shape, nothing else:
{
  "entities": ["Company Name", ...],
  "dataPoints": [
    {
      "entity": "who/what this data point is about",
      "metric": "what is being measured, e.g. Revenue, Funding Raised, YoY Growth",
      "value": "the number or short value, as stated in the source",
      "unit": "e.g. USD million, %, INR crore, x (multiple) — empty string if none",
      "period": "e.g. FY25, Q1 2026, 2026 — empty string if not stated",
      "sourceTitle": "title of the source article",
      "sourceUrl": "url of the source article"
    }
  ]
}
Extract every usable data point you find in these sources — do not hold back \
for length."""


def _fundamentals_to_datapoints(stocks: list[dict]) -> list[dict]:
    """Converts utils.market_data.fetch_stock_fundamentals() output into flat
    data-point rows — no LLM in this path, so these numbers are exact."""
    field_map = [
        ("price", "Price", lambda s: s.get("currency") or ""),
        ("marketCap", "Market Cap", lambda s: s.get("currency") or ""),
        ("peRatio", "Trailing P/E", lambda s: "x"),
        ("forwardPE", "Forward P/E", lambda s: "x"),
        ("priceToBook", "Price to Book", lambda s: "x"),
        ("debtToEquity", "Debt to Equity", lambda s: ""),
        ("returnOnEquityPct", "Return on Equity", lambda s: "%"),
        ("profitMarginPct", "Net Profit Margin", lambda s: "%"),
        ("revenueGrowthPct", "Revenue Growth (YoY)", lambda s: "%"),
        ("earningsGrowthPct", "Earnings Growth (YoY)", lambda s: "%"),
        ("epsTTM", "EPS (TTM)", lambda s: ""),
        ("dividendYieldPct", "Dividend Yield", lambda s: "%"),
    ]
    out: list[dict] = []
    for s in stocks:
        name = f"{s.get('name', '')} ({s.get('symbol', '')})".strip()
        for key, label, unit_fn in field_map:
            val = s.get(key)
            if val is None:
                continue
            out.append({
                "entity": name,
                "metric": label,
                "value": val,
                "unit": unit_fn(s),
                "period": "Live",
                "sourceTitle": "Live Market Data",
                "sourceUrl": "",
                "kind": "live",
            })
    return out


async def _expand_query(query: str, num_angles: int) -> list[str]:
    """Best-effort query expansion. Falls back to the original query alone
    (single-angle, same as before) if the LLM call fails or returns junk —
    this step is purely additive, never a hard dependency."""
    if num_angles <= 1:
        return [query]
    parsed = await extract_json(
        _QUERY_EXPANSION_SYSTEM_PROMPT,
        f"USER REQUEST:\n{query}\n\nGenerate up to {num_angles} search queries.",
    )
    queries = (parsed or {}).get("queries") if isinstance(parsed, dict) else None
    if not isinstance(queries, list):
        return [query]
    cleaned = [str(q).strip()[:400] for q in queries if str(q).strip()][:num_angles]
    return cleaned or [query]


def _clean_data_points(raw_points: list, kind: str) -> list[dict]:
    out: list[dict] = []
    for p in raw_points:
        if not isinstance(p, dict):
            continue
        entity = str(p.get("entity", "")).strip()
        metric = str(p.get("metric", "")).strip()
        value = p.get("value", "")
        if not entity or not metric or value in ("", None):
            continue
        out.append({
            "entity": entity,
            "metric": metric,
            "value": value,
            "unit": str(p.get("unit", "") or ""),
            "period": str(p.get("period", "") or ""),
            "sourceTitle": str(p.get("sourceTitle", "") or ""),
            "sourceUrl": str(p.get("sourceUrl", "") or ""),
            "kind": kind,
        })
    return out


async def _extract_batch(query: str, batch: list[dict]) -> dict:
    """Runs one JSON-extraction LLM call over a bounded batch of search
    results. Prefers the fuller fullContent over the short snippet — at
    BATCH_SIZE=50 and 4000 chars/source a batch is still only ~50K tokens,
    comfortably inside Groq's context and a small fraction of Gemini's."""
    parts = [f"USER REQUEST:\n{query}\n\nSEARCH RESULTS:"]
    for i, r in enumerate(batch, 1):
        content = (r.get("fullContent") or r.get("snippet") or "")[:4000]
        parts.append(f"\n[{i}] {r.get('title', '')}\nURL: {r.get('url', '')}\n{content}")
    parsed = await extract_json(_EXTRACTION_SYSTEM_PROMPT, "\n".join(parts))
    return parsed if isinstance(parsed, dict) else {}


@router.post("")
async def datasearch(request: Request):
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    num_keys = len(get_tavily_keys())
    num_angles = max(1, min(MAX_ANGLES, num_keys or 1))
    log.info("datasearch: query=%r  tavily_keys=%d  angles=%d", query[:120], num_keys, num_angles)

    angles = await _expand_query(query, num_angles)
    log.info("datasearch: expanded into %d angle(s): %r", len(angles), [a[:60] for a in angles])

    search_results = await tavily_search_multi(angles, max_results=RESULTS_PER_ANGLE, min_results=10)
    search_results = search_results[: num_angles * RESULTS_PER_ANGLE]
    log.info("datasearch: %d unique source URL(s) across %d angle(s)", len(search_results), len(angles))

    sources = [
        {"title": r.get("title", ""), "url": r.get("url", "")}
        for r in search_results if r.get("url")
    ]

    batches = [search_results[i:i + BATCH_SIZE] for i in range(0, len(search_results), BATCH_SIZE)] or [[]]
    batch_results = await asyncio.gather(*[_extract_batch(query, b) for b in batches])

    entities: list[str] = []
    web_points: list[dict] = []
    for parsed in batch_results:
        batch_entities = parsed.get("entities") or []
        if isinstance(batch_entities, list):
            entities.extend(str(e).strip() for e in batch_entities if str(e).strip())
        raw_points = parsed.get("dataPoints") or []
        if isinstance(raw_points, list):
            web_points.extend(_clean_data_points(raw_points, "web"))

    # de-dupe entities case-insensitively, preserve first-seen order, cap at
    # 6 — fetch_stock_fundamentals already caps there internally
    seen_entities: set[str] = set()
    unique_entities: list[str] = []
    for e in entities:
        key = e.lower()
        if key not in seen_entities:
            seen_entities.add(key)
            unique_entities.append(e)
    unique_entities = unique_entities[:6]

    live_points: list[dict] = []
    if unique_entities:
        try:
            stocks = await fetch_stock_fundamentals(unique_entities)
            live_points = _fundamentals_to_datapoints(stocks)
        except Exception as exc:
            log.warning("datasearch: fundamentals lookup failed: %s", exc)

    data_points = (live_points + web_points)[:MAX_DATA_POINTS]

    return JSONResponse({
        "query": query,
        "dataPoints": data_points,
        "sources": sources,
        "sourceCount": len(sources),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    })
