"""
POST /api/datasearch — turn a free-text financial question into structured
data points for the frontend's Data Search Engine (search/page.tsx ->
DataDashboard.tsx: bar charts per metric, a sortable table, Excel/CSV export).

Body: { query: str }
Response: { query, dataPoints: DataPoint[], sourceCount, sources }
  DataPoint = { entity, metric, value, unit?, period?, sourceTitle?, sourceUrl?, kind? }

Coverage (best-effort, additive — a source that yields nothing is simply
omitted, same contract as the rest of this backend):
  1. Screener.in knowledge base — richest source when a company resolves:
     ratios, multi-period quarterly/annual financials, growth CAGR.
  2. Live Yahoo fundamentals — fills in any named company not present in
     the Screener KB (e.g. very recently listed, or an ADR/foreign name).
  3. Live index quotes — when the query is clearly about NIFTY/SENSEX/etc.
  4. Open-ended web research — for anything the above three can't resolve
     (e.g. "funding raised by top Indian fintech startups"). This is a real
     multi-round research loop, not a single search-and-extract pass:
       round 1: expand the question into up to 5 focused search angles,
                search each, extract concrete numeric data points from the
                (often full-page-enriched — see routes/chat._enrich_thin_
                results) retrieved content.
       gap check: an LLM reviews what was actually found against the
                original question and either says "sufficient" or proposes
                up to 5 new, gap-targeted queries (a named entity with no
                number yet, a metric asked for but missing, a more recent
                period) — never a blind repeat of round 1.
       round 2/3: search + extract again with the gap-targeted queries,
                merged into the running result set. Stops early once the
                gap check is satisfied, or after _MAX_ROUNDS regardless —
                so a narrow query (1-2 companies, one metric) typically
                finishes in one round, while a broad one uses more.
     Same-entity/metric figures that disagree across sources are never
     silently overwritten — see _values_close/_disambiguate_conflict below.
"""
import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from utils.screener_kb import fetch_screener_fundamentals
from utils.market_data import fetch_stock_fundamentals, fetch_index_quotes
from utils.websearch import search_web, web_search_available
from utils.keys import get_groq_keys, get_gemini_keys, round_robin, is_rate_limited, mark_rate_limited
from routes.report import _extract_company_candidates, _extract_json_object, _is_rest_api_key, GEMINI_MODELS

router = APIRouter()
log = logging.getLogger("datasearch")

# How many distinct search angles per round, and how many results to pull
# per angle — 5 x 20 = up to 100 sources per round, matching the frontend's
# loading copy ("Scanning up to 100 sources...").
_MAX_SEARCH_ANGLES = 5
_RESULTS_PER_ANGLE = 20
# Cap on how many of the merged/deduped results actually get sent to the
# extraction LLM per round — keeps the prompt (and Groq's 413 risk) bounded
# even when search_web() returns close to the full 100.
_MAX_SOURCES_FOR_EXTRACTION = 30
# Sources are often already full-page-enriched (tavily_search enriches thin
# results up to ~3000 chars before this module ever sees them), so this
# isn't a hard content ceiling — it's how much of that richer content we
# actually forward to the extraction prompt per source.
_SNIPPET_CHARS = 1200
# Research loop depth — 1 round handles a narrow query fine; the gap check
# after each round decides whether a 2nd/3rd round is actually warranted,
# so this is a ceiling for broad questions, not a fixed cost every query pays.
_MAX_ROUNDS = 3
# A same-entity/metric value from a later round/source that differs from an
# already-recorded one by more than this fraction is treated as a genuine
# disagreement (kept as a separate, disambiguated point) rather than a
# duplicate confirmation (dropped).
_CONFLICT_REL_TOL = 0.02
# Hard ceiling on the whole multi-round research loop, regardless of how
# many Groq/Gemini keys _llm_json ends up cycling through internally or how
# many rounds run. Without this, a run of slow/rate-limited keys across 3
# rounds can add up past the frontend's 170s budget and take the entire
# response down with it — not just the web-research portion of it.
_WEB_RESEARCH_DEADLINE_S = 140

_INDEX_INTENT_RE = re.compile(
    r"\b(nifty|sensex|bse|nse|bank nifty|market today|indices|indian stock market)\b",
    re.IGNORECASE,
)

# Key financial-statement line items worth surfacing per period — kept short
# so the dashboard's table/charts stay readable rather than dumping every
# line item screener tracks.
_KEY_FINANCIALS = [
    ("Quarterly", "Sales"),
    ("Quarterly", "Net Profit"),
    ("Annual P&L", "Sales"),
    ("Annual P&L", "Net Profit"),
    ("Balance Sheet", "Total Assets"),
]


def _base_ticker(symbol: str) -> str:
    """Strips exchange suffixes so 'RELIANCE.NS' and 'RELIANCE' compare equal."""
    return re.sub(r"\.(NS|BO)$", "", (symbol or "").upper())


def _screener_datapoints(snap: dict) -> list[dict]:
    c = snap.get("company") or {}
    name = c.get("name") or c.get("ticker") or "Unknown"
    url = c.get("company_url")
    points = []

    for r in snap.get("ratios") or []:
        val = r.get("raw_value") or r.get("value")
        if r.get("metric") and val is not None:
            points.append({
                "entity": name, "metric": r["metric"], "value": val,
                "sourceTitle": "Screener.in", "sourceUrl": url, "kind": "live",
            })

    fin = snap.get("financials") or []
    for statement, item in _KEY_FINANCIALS:
        rows = [r for r in fin if r.get("statement") == statement and r.get("line_item") == item][:6]
        for r in rows:
            val = r.get("raw_value") or r.get("value")
            if val is None:
                continue
            points.append({
                "entity": name, "metric": f"{statement} — {item}", "value": val,
                "period": r.get("period"),
                "sourceTitle": "Screener.in", "sourceUrl": url, "kind": "live",
            })

    for g in snap.get("growth_cagr") or []:
        val = g.get("raw_value") or g.get("value")
        if g.get("metric") and val is not None:
            points.append({
                "entity": name, "metric": f"CAGR — {g['metric']}", "value": val,
                "period": g.get("period"),
                "sourceTitle": "Screener.in", "sourceUrl": url, "kind": "live",
            })

    return points


def _yahoo_datapoints(stock: dict) -> list[dict]:
    name = stock.get("name") or stock.get("symbol")
    symbol = stock.get("symbol")
    url = f"https://finance.yahoo.com/quote/{symbol}" if symbol else None
    fields = [
        ("price", "CMP", stock.get("currency") or ""),
        ("marketCap", "Market Cap", stock.get("currency") or ""),
        ("peRatio", "Trailing P/E", "x"),
        ("forwardPE", "Forward P/E", "x"),
        ("priceToBook", "P/B", "x"),
        ("debtToEquity", "Debt-to-Equity", ""),
        ("returnOnEquityPct", "ROE", "%"),
        ("profitMarginPct", "Net Margin", "%"),
        ("revenueGrowthPct", "Revenue Growth (YoY)", "%"),
        ("earningsGrowthPct", "Earnings Growth (YoY)", "%"),
        ("epsTTM", "EPS (TTM)", ""),
        ("dividendYieldPct", "Dividend Yield", "%"),
    ]
    points = []
    for key, label, unit in fields:
        val = stock.get(key)
        if val is not None:
            points.append({
                "entity": name, "metric": label, "value": round(val, 2) if isinstance(val, float) else val,
                "unit": unit or None,
                "sourceTitle": "Yahoo Finance", "sourceUrl": url, "kind": "live",
            })
    return points


async def _llm_json(system_prompt: str, user_prompt: str, *, max_tokens: int, timeout: float) -> dict | None:
    """Groq (JSON mode) -> Gemini (best-effort JSON) for the two LLM steps in
    the open-ended web-research path (query expansion, datapoint extraction).

    Deliberately separate from routes.report._call_llm_json rather than
    reusing it directly: that helper hard-codes a 10s httpx timeout and a
    2048-token cap sized for single-chart edits, which is too tight for an
    extraction pass over ~40 web sources. Same model choice and JSON-salvage
    behavior, just with budgets sized for this job.
    """
    for key in round_robin(get_groq_keys()):
        if is_rate_limited(key):
            continue
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                    json={
                        "model": "openai/gpt-oss-120b",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                    },
                )
            if res.status_code == 200:
                content = res.json()["choices"][0]["message"]["content"]
                parsed = _extract_json_object(content)
                if parsed is not None:
                    return parsed
            elif res.status_code == 429:
                mark_rate_limited(key, 60_000)
            elif res.status_code == 413:
                log.warning("datasearch: Groq 413 (payload too large) — falling through to Gemini")
                break
        except Exception as e:
            log.warning("datasearch: Groq JSON call failed on key ...%s: %s", key[-4:], e)

    for key in round_robin(get_gemini_keys()):
        if not _is_rest_api_key(key) or is_rate_limited(key):
            continue
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELS[0]}:generateContent?key={key}",
                    json={
                        "contents": [{"role": "user", "parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
                        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"},
                    },
                )
            if res.status_code == 200:
                content = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                parsed = _extract_json_object(content)
                if parsed is not None:
                    return parsed
            elif res.status_code == 429:
                mark_rate_limited(key, 60_000)
        except Exception as e:
            log.warning("datasearch: Gemini JSON call failed on key ...%s: %s", key[-4:], e)

    return None


async def _expand_search_angles(query: str) -> list[str]:
    """Turns one free-text question into up to _MAX_SEARCH_ANGLES focused
    search queries, so a broad ask (e.g. "funding raised by top Indian
    fintech startups in 2026") becomes several angles a search engine can
    actually answer (e.g. per-company funding rounds, a roundup article
    query, a specific recent-quarter query) instead of one vague query that
    mostly returns overview/explainer pages with no numbers in them.
    Falls back to the original query, unexpanded, on any failure — this is
    an enrichment step, never a hard dependency for the search to run.
    """
    system = (
        "You turn one research question about Indian financial markets/companies "
        "into a JSON list of focused web search queries. "
        f"Return ONLY {{\"queries\": [string, ...]}} with 1 to {_MAX_SEARCH_ANGLES} items. "
        "Each query should target a distinct angle likely to surface concrete numbers "
        "(specific companies, a recent time period, a named data source/report), "
        "not a restatement of the question. If the question is already narrow "
        "(e.g. names one or two companies and a specific metric), return just that "
        "one query rather than inventing angles."
    )
    result = await _llm_json(system, query, max_tokens=512, timeout=15)
    angles = result.get("queries") if isinstance(result, dict) else None
    if isinstance(angles, list):
        cleaned = [a.strip() for a in angles if isinstance(a, str) and a.strip()]
        if cleaned:
            return cleaned[:_MAX_SEARCH_ANGLES]
    return [query]


def _domain(url: str | None) -> str:
    try:
        return (urlparse(url or "").netloc or "").removeprefix("www.")
    except Exception:
        return ""


def _values_close(a, b) -> bool:
    """True if two numeric values agree closely enough to treat a repeat
    mention as confirmation rather than a genuine conflicting figure."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if a == b:
        return True
    if a == 0 or b == 0:
        return abs(a - b) < 1e-9
    return abs(a - b) / max(abs(a), abs(b)) <= _CONFLICT_REL_TOL


async def _extract_round_datapoints(query: str, results: list[dict]) -> list[dict]:
    """One round's search->extract step. Returns raw DataPoint dicts (kind
    already set to 'web', sourceTitle/sourceUrl taken from the real result —
    never from the LLM) for the given already-fetched search results."""
    if not results:
        return []
    sources = results[:_MAX_SOURCES_FOR_EXTRACTION]
    source_block = "\n".join(
        f"[{i}] {r.get('title', '')} — {r.get('url', '')}\n"
        f"{(r.get('fullContent') or r.get('snippet') or '')[:_SNIPPET_CHARS]}"
        for i, r in enumerate(sources)
    )

    system = (
        "You extract concrete, explicitly-stated numeric data points from web search "
        "content to populate a research dashboard. Return ONLY JSON: "
        "{\"dataPoints\": [{\"sourceIndex\": int, \"entity\": string, \"metric\": string, "
        "\"value\": number, \"unit\": string|null, \"period\": string|null}]}. Rules: "
        "(1) Only include a data point if a specific number for it is explicitly present in "
        "the numbered source text below — never estimate, infer, or use outside knowledge. "
        "(2) sourceIndex must be the exact [n] the number came from. "
        "(3) entity is the company/fund/index/subject the number is about; metric is what was "
        "measured (e.g. 'Funding Raised', 'AUM', 'Revenue', 'Valuation'). "
        "(4) unit is a currency/percent/count label (e.g. 'USD mn', 'Rs Cr', '%') or null. "
        "(5) period is the time period the number applies to (e.g. 'FY25', 'Q2 2026') or null. "
        "(6) Skip vague or duplicate figures; prefer the most recent and most specific "
        "numbers. Return at most 40 data points. If nothing qualifies, return an empty list."
    )
    user = f"Research question: {query}\n\nNumbered sources:\n{source_block}"
    extracted = await _llm_json(system, user, max_tokens=4096, timeout=45)

    raw_points = extracted.get("dataPoints") if isinstance(extracted, dict) else None
    if not isinstance(raw_points, list):
        return []

    points = []
    for p in raw_points:
        if not isinstance(p, dict):
            continue
        idx = p.get("sourceIndex")
        if not isinstance(idx, int) or not (0 <= idx < len(sources)):
            continue
        val = p.get("value")
        if not isinstance(val, (int, float)) or not p.get("entity") or not p.get("metric"):
            continue
        src = sources[idx]
        points.append({
            "entity": str(p["entity"])[:120],
            "metric": str(p["metric"])[:80],
            "value": val,
            "unit": p.get("unit") if isinstance(p.get("unit"), str) else None,
            "period": p.get("period") if isinstance(p.get("period"), str) else None,
            "sourceTitle": src.get("title") or "Web Search",
            "sourceUrl": src.get("url"),
            "kind": "web",
        })
    return points


async def _assess_gaps(query: str, tried_queries: list[str], points_so_far: list[dict]) -> dict | None:
    """After a round of research, ask an LLM whether what's been found
    actually answers the question, or whether another round targeting
    specific gaps would help. This is what makes the loop adaptive instead
    of a fixed number of rounds for every query — a narrow question that's
    already answered stops after round 1; a broad one keeps going with
    queries aimed at exactly what's missing, not a blind repeat.
    Returns None on failure (treated by the caller as "stop here" — this is
    an enrichment decision, never a hard requirement to keep researching).
    """
    preview = "\n".join(
        f"- {p['entity']}: {p['metric']} = {p['value']}{(' ' + p['unit']) if p.get('unit') else ''}"
        f"{(' (' + p['period'] + ')') if p.get('period') else ''}"
        for p in points_so_far[:60]
    ) or "(none found yet)"
    tried = "\n".join(f"- {q}" for q in tried_queries) or "(none)"

    system = (
        "You review partial research results against a research question and decide whether "
        "another round of web search would meaningfully add missing numeric data. Return ONLY "
        "JSON: {\"sufficient\": bool, \"next_queries\": [string, ...]}. "
        "\"sufficient\": true means the data points already found adequately answer the "
        "question — stop here. If false, give up to 5 NEW, specific search queries that target "
        "exactly what's still missing (a named entity mentioned in the question but with no "
        "number yet, a metric the question asked for but that's absent, a more recent time "
        "period, a specific report/source likely to have it) — never repeat a query already "
        "tried, and never invent queries for things the question didn't ask about."
    )
    user = (
        f"Research question: {query}\n\n"
        f"Queries already tried:\n{tried}\n\n"
        f"Data points found so far ({len(points_so_far)}):\n{preview}"
    )
    return await _llm_json(system, user, max_tokens=768, timeout=15)


async def _web_research_datapoints(query: str) -> tuple[list[dict], int, int]:
    """Open-ended, multi-round research loop: expand -> search -> extract,
    then a gap check decides whether to run another gap-targeted round.
    Returns (dataPoints, sourcesScanned, roundsRun). Never raises — any
    failure here just means this source contributes nothing, same as the
    other three sources in this file.
    """
    if not web_search_available():
        log.info("datasearch: web search not configured — skipping web-research pass")
        return [], 0, 0

    all_points: list[dict] = []
    value_by_key: dict[tuple[str, str], float] = {}
    tried_queries: list[str] = []
    sources_scanned = 0
    next_queries: list[str] | None = None
    rounds_run = 0

    for round_idx in range(_MAX_ROUNDS):
        rounds_run = round_idx + 1
        if round_idx == 0:
            try:
                angles = await _expand_search_angles(query)
            except Exception as e:
                log.warning("datasearch: query expansion failed, using raw query: %s", e)
                angles = [query]
        else:
            angles = next_queries or []
        angles = [a for a in angles if a not in tried_queries]
        if not angles:
            break
        tried_queries.extend(angles)

        search = await search_web(angles, max_results=_RESULTS_PER_ANGLE, topic="finance")
        results = search.get("results") or []
        sources_scanned += min(len(results), _MAX_SOURCES_FOR_EXTRACTION)
        if not results:
            if round_idx == 0:
                return [], 0, rounds_run
            break  # a gap-targeted round found nothing new — no point continuing

        round_points = await _extract_round_datapoints(query, results)

        for p in round_points:
            key = (p["entity"].strip().lower(), p["metric"].strip().lower())
            existing = value_by_key.get(key)
            if existing is None:
                value_by_key[key] = p["value"]
                all_points.append(p)
            elif not _values_close(existing, p["value"]):
                # Genuine disagreement between sources — surface both rather
                # than silently keeping whichever was extracted first, so
                # the dashboard shows the conflict instead of hiding it.
                domain = _domain(p.get("sourceUrl"))
                p["entity"] = f"{p['entity']} ({domain})" if domain else f"{p['entity']} (alt. source)"
                all_points.append(p)
            # else: same figure confirmed by another source — skip, not new info

        if round_idx == _MAX_ROUNDS - 1:
            break
        try:
            gap = await _assess_gaps(query, tried_queries, all_points)
        except Exception as e:
            log.warning("datasearch: gap assessment failed, stopping research loop: %s", e)
            break
        if not isinstance(gap, dict) or gap.get("sufficient"):
            break
        candidates = gap.get("next_queries")
        if not isinstance(candidates, list):
            break
        next_queries = [q.strip() for q in candidates if isinstance(q, str) and q.strip()][:_MAX_SEARCH_ANGLES]
        if not next_queries:
            break

    return all_points, sources_scanned, rounds_run


@router.post("")
async def datasearch(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    query = (body.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "Missing query"}, status_code=400)

    data_points: list[dict] = []
    resolved_tickers: set[str] = set()

    candidates = _extract_company_candidates(query)
    if candidates:
        try:
            snaps = await fetch_screener_fundamentals(candidates)
            for snap in snaps:
                data_points.extend(_screener_datapoints(snap))
                ticker = (snap.get("company") or {}).get("ticker")
                if ticker:
                    resolved_tickers.add(_base_ticker(ticker))
        except Exception as e:
            log.warning("datasearch: screener KB fetch failed: %s", e)

        try:
            stocks = await fetch_stock_fundamentals(candidates)
            for s in stocks:
                if _base_ticker(s.get("symbol", "")) in resolved_tickers:
                    continue  # already covered by the richer Screener KB snapshot
                data_points.extend(_yahoo_datapoints(s))
        except Exception as e:
            log.warning("datasearch: Yahoo fundamentals fetch failed: %s", e)

    if _INDEX_INTENT_RE.search(query):
        try:
            quotes = await fetch_index_quotes()
            for q in quotes:
                data_points.append({
                    "entity": q["label"], "metric": "Index Level", "value": round(q["price"], 2),
                    "sourceTitle": "Live Market Data", "sourceUrl": None, "kind": "live",
                })
                if q.get("changePct") is not None:
                    data_points.append({
                        "entity": q["label"], "metric": "Change %", "value": round(q["changePct"], 2), "unit": "%",
                        "sourceTitle": "Live Market Data", "sourceUrl": None, "kind": "live",
                    })
        except Exception as e:
            log.warning("datasearch: index quote fetch failed: %s", e)

    # Open-ended web research — additive, and the only path for anything the
    # three structured sources above don't cover (see module docstring).
    # Runs regardless of whether a company/index already resolved, since a
    # research query can legitimately mix a known company with open-ended
    # context (e.g. "how does Zomato's growth compare to funding raised by
    # other Indian food-delivery startups") — deduped against what the live
    # sources already surfaced so the dashboard doesn't show the same
    # entity+metric twice.
    try:
        web_points, sources_scanned, rounds_run = await asyncio.wait_for(
            _web_research_datapoints(query), timeout=_WEB_RESEARCH_DEADLINE_S,
        )
        already_covered = {(p["entity"].strip().lower(), p["metric"].strip().lower()) for p in data_points}
        for wp in web_points:
            key = (wp["entity"].strip().lower(), wp["metric"].strip().lower())
            if key not in already_covered:
                already_covered.add(key)
                data_points.append(wp)
        log.info("datasearch: web research ran %d round(s), scanned %d source(s) -> %d new data point(s)",
                  rounds_run, sources_scanned, len(web_points))
    except asyncio.TimeoutError:
        log.warning("datasearch: web research pass exceeded %ds deadline — returning live sources only",
                    _WEB_RESEARCH_DEADLINE_S)
    except Exception as e:
        log.warning("datasearch: web research pass failed: %s", e)

    sources = []
    seen = set()
    for p in data_points:
        key = (p.get("sourceTitle"), p.get("sourceUrl"))
        if p.get("sourceTitle") and key not in seen:
            seen.add(key)
            sources.append({"title": p["sourceTitle"], "url": p.get("sourceUrl") or ""})

    log.info("datasearch: query=%r -> %d data point(s) from %d source(s)",
              query[:80], len(data_points), len(sources))

    return {
        "query": query,
        "dataPoints": data_points,
        "sourceCount": len(sources),
        "sources": sources,
    }
