"""
POST /api/datasearch — turn a free-text financial question into structured
data points for the frontend's Data Search Engine (search/page.tsx ->
DataDashboard.tsx: bar charts per metric, a sortable table, Excel/CSV export).

Body: { query: str }
Response: { query, dataPoints: DataPoint[], sourceCount, sources }
  DataPoint = { entity, metric, value, unit?, period?, sourceTitle?, sourceUrl?, kind? }

Current coverage (best-effort, additive — a source that yields nothing is
simply omitted, same contract as the rest of this backend):
  1. Screener.in knowledge base — richest source when a company resolves:
     ratios, multi-period quarterly/annual financials, growth CAGR.
  2. Live Yahoo fundamentals — fills in any named company not present in
     the Screener KB (e.g. very recently listed, or an ADR/foreign name).
  3. Live index quotes — when the query is clearly about NIFTY/SENSEX/etc.

Open-ended non-company questions (e.g. "funding raised by fintech
startups") aren't covered yet — that needs an LLM-driven extraction pass
over web search results, which is a larger follow-up piece, not a gap in
this file's wiring.
"""
import logging
import re

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from utils.screener_kb import fetch_screener_fundamentals
from utils.market_data import fetch_stock_fundamentals, fetch_index_quotes
from routes.report import _extract_company_candidates

router = APIRouter()
log = logging.getLogger("datasearch")

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
