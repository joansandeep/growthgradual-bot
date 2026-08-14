"""
Verified index-quote fetcher.

Mirrors the strategy already used by frontend/src/app/api/market/route.ts
(Yahoo Finance v8 chart endpoint, no auth required; Stooq CSV as a free
no-auth fallback) but as a small async Python helper the report pipeline can
call directly, so index numbers going into a report/chart come from a real
quote instead of whatever a Tavily/Trendlyne snippet happened to contain.

If both providers fail for a symbol, it's simply omitted — callers should
treat this as best-effort and fall back to scraped sources when empty.
"""
import logging
from typing import Optional

import httpx

log = logging.getLogger("market_data")

YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://finance.yahoo.com/",
}

# stooq symbol, yahoo symbol, display label
INDEX_SYMBOLS = [
    ("^nsei", "^NSEI", "NIFTY 50"),
    ("^bsesn", "^BSESN", "SENSEX"),
    ("^nsebank", "^NSEBANK", "NIFTY BANK"),
    ("^cnxit", "^CNXIT", "NIFTY IT"),
    ("^nseminidcap50", "^NSEMDCP50", "NIFTY MIDCAP"),  # was ^CNXMID — not a real Yahoo ticker, always 404'd
]


async def _yf_chart_quote(client: httpx.AsyncClient, yf_symbol: str) -> Optional[dict]:
    """Yahoo Finance v8 chart endpoint — no crumb/cookie needed."""
    try:
        r = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}",
            params={"interval": "1d", "range": "1d"},
            headers=YF_HEADERS,
            timeout=8.0,
        )
        if r.status_code != 200:
            return None
        meta = (r.json().get("chart", {}).get("result") or [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if not price:
            return None
        prev = meta.get("chartPreviousClose", price)
        change_pct = ((price - prev) / prev * 100) if prev else 0.0
        return {"price": price, "changePct": change_pct}
    except Exception as e:
        log.debug("yf_chart_quote failed for %s: %s", yf_symbol, e)
        return None


async def _stooq_quote(client: httpx.AsyncClient, stooq_symbol: str) -> Optional[dict]:
    """Stooq CSV — free, no auth, reliable fallback when Yahoo rate-limits."""
    try:
        r = await client.get(
            "https://stooq.com/q/l/",
            params={"s": stooq_symbol, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8.0,
        )
        lines = r.text.strip().splitlines()
        if len(lines) < 2:
            return None
        cols = lines[1].split(",")
        close, open_ = float(cols[6]), float(cols[4])
        if not close or not open_:
            return None
        return {"price": close, "changePct": (close - open_) / open_ * 100}
    except Exception as e:
        log.debug("stooq_quote failed for %s: %s", stooq_symbol, e)
        return None


async def _yf_chart_history(
    client: httpx.AsyncClient, yf_symbol: str, range_str: str, interval: str,
) -> Optional[list[dict]]:
    """Yahoo Finance v8 chart endpoint, same as _yf_chart_quote but requesting
    a wider range/interval (e.g. range=2y&interval=3mo) so we get a real
    series of period-end closes instead of a single day's price. Yahoo's
    documented intraday/interval values include 3mo, so an ~8-quarter window
    maps naturally onto range=2y&interval=3mo — each bucket is one point.
    """
    try:
        r = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}",
            params={"interval": interval, "range": range_str},
            headers=YF_HEADERS,
            timeout=10.0,
        )
        if r.status_code != 200:
            return None
        result = (r.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return None
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []

        points: list[dict] = []
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            # Label by the bucket's start month/year rather than inventing a
            # fiscal-quarter number (e.g. "Q2 FY25") — India's fiscal year
            # (Apr-Mar) means a naive Q1/Q2/Q3/Q4 label could easily be wrong
            # by one quarter depending on where the 2y window starts. A plain
            # "Jul 2024"-style label is unambiguous and lets the report
            # writer map it onto whatever quarter framing the question used.
            from datetime import datetime, timezone
            label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %Y")
            points.append({"date": label, "close": round(float(close), 2)})
        return points or None
    except Exception as e:
        log.debug("yf_chart_history failed for %s: %s", yf_symbol, e)
        return None


async def fetch_historical_index_quotes(
    range_str: str = "2y", interval: str = "3mo",
) -> list[dict]:
    """Returns a real historical close series per major Indian index, e.g.:
        [{"label": "NIFTY 50", "points": [{"date": "Jul 2024", "close": 24812.35}, ...]}, ...]
    Yahoo-only — Stooq's free endpoint only gives the latest single quote, not
    a clean multi-point history, so it isn't used here as a fallback the way
    it is in fetch_index_quotes(). If Yahoo fails for a symbol (or returns
    fewer than 2 usable points, which isn't enough to show a trend), that
    symbol is simply omitted — same best-effort contract as the live fetcher.
    """
    out: list[dict] = []
    async with httpx.AsyncClient() as client:
        for _, yf_sym, label in INDEX_SYMBOLS:
            points = await _yf_chart_history(client, yf_sym, range_str, interval)
            if points and len(points) >= 2:
                out.append({"label": label, "points": points})
    log.info("market_data: fetched historical series for %d/%d indices (range=%s, interval=%s)",
              len(out), len(INDEX_SYMBOLS), range_str, interval)
    return out


def format_historical_quotes_as_source(series: list[dict]) -> Optional[dict]:
    """Wraps a fetched historical series as a synthetic, high-trust "source"
    dict — same shape/contract as format_quotes_as_source() below, but for
    period-over-period data instead of a single day's snapshot."""
    if not series:
        return None
    lines = []
    for s in series:
        pts = s["points"]
        parts = [f"{pts[0]['date']}: {pts[0]['close']:.2f}"]
        for prev, cur in zip(pts, pts[1:]):
            chg = ((cur["close"] - prev["close"]) / prev["close"] * 100) if prev["close"] else 0.0
            parts.append(f"{cur['date']}: {cur['close']:.2f} ({chg:+.2f}% vs prior period)")
        lines.append(f"{s['label']} — " + "; ".join(parts))
    return {
        "title": (
            "VERIFIED HISTORICAL INDEX DATA (authoritative — real period-end closing "
            "levels from Yahoo Finance, use these exact figures for any quarter-over-quarter "
            "or period comparison; each period is an ~3-month bucket labelled by its start "
            "month — map it to a fiscal quarter in prose if needed, but do not alter the "
            "underlying figures)"
        ),
        "url": "internal://verified-historical-index-quotes",
        "snippet": " | ".join(lines)[:2000],
        "fullContent": "\n".join(lines),
    }


async def fetch_index_quotes() -> list[dict]:
    """Returns verified quotes for the major Indian indices, e.g.:
        [{"label": "NIFTY 50", "price": 24812.35, "changePct": 0.42}, ...]
    Tries Yahoo first, falls back to Stooq per-symbol. Silently omits any
    symbol both providers fail on rather than raising.
    """
    out: list[dict] = []
    async with httpx.AsyncClient() as client:
        for stooq_sym, yf_sym, label in INDEX_SYMBOLS:
            quote = await _yf_chart_quote(client, yf_sym)
            if not quote:
                quote = await _stooq_quote(client, stooq_sym)
            if quote:
                out.append({"label": label, **quote})
    log.info("market_data: fetched %d/%d index quotes", len(out), len(INDEX_SYMBOLS))
    return out


async def _yf_symbol_search(client: httpx.AsyncClient, company_name: str) -> Optional[dict]:
    """Resolve a free-text company name (e.g. 'Reliance Industries') to a real
    ticker via Yahoo's unauthenticated symbol-search endpoint. This is what
    lets stock-fundamentals lookup generalize to ANY company mentioned in a
    prompt instead of only a hard-coded list — same no-crumb-needed contract
    as the chart endpoint above.
    """
    try:
        r = await client.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": company_name, "quotesCount": 5, "newsCount": 0},
            headers=YF_HEADERS,
            timeout=8.0,
        )
        if r.status_code != 200:
            return None
        for q in r.json().get("quotes", []):
            # EQUITY only — skip ETFs/indices/crypto/futures matches so a name
            # like "Tata Motors" doesn't resolve to some unrelated fund.
            if q.get("quoteType") == "EQUITY" and q.get("symbol"):
                return {"symbol": q["symbol"], "name": q.get("shortname") or q.get("longname") or company_name}
        return None
    except Exception as e:
        log.debug("yf_symbol_search failed for %r: %s", company_name, e)
        return None


_yf_crumb_cache: dict[str, str] = {}  # "crumb" -> value; module-level, one process-lifetime crumb is enough


async def _get_yf_crumb(client: httpx.AsyncClient) -> Optional[str]:
    """quoteSummary (unlike the v8 chart endpoint) has required a
    cookie + crumb pair since Yahoo locked it down in 2024 — calling it with
    just a User-Agent now gets an unconditional 401, which is exactly what
    was happening here before (every /v10/finance/quoteSummary/... call
    failing with 401, so market_data always resolved 0 fundamentals).

    Fetches an A1/A3 session cookie from the public finance.yahoo.com page,
    then exchanges it for a crumb via the getcrumb endpoint. Both are cached
    on the client's cookie jar / this module for the process lifetime — the
    crumb doesn't need to be re-fetched per request.
    """
    if _yf_crumb_cache.get("crumb"):
        return _yf_crumb_cache["crumb"]
    try:
        # Populates client.cookies with the session cookie Yahoo expects.
        await client.get("https://fc.yahoo.com", headers=YF_HEADERS, timeout=8.0)
        r = await client.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            headers=YF_HEADERS,
            timeout=8.0,
        )
        crumb = r.text.strip()
        if r.status_code == 200 and crumb and "<html" not in crumb.lower():
            _yf_crumb_cache["crumb"] = crumb
            return crumb
    except Exception as e:
        log.debug("yf_get_crumb failed: %s", e)
    return None


async def _yf_quote_summary(client: httpx.AsyncClient, symbol: str) -> Optional[dict]:
    """Pulls valuation/debt/profitability fundamentals for one resolved
    ticker via the v10 quoteSummary endpoint (price + summaryDetail +
    defaultKeyStatistics + financialData modules). Best-effort: any missing
    field is simply omitted rather than faked, same contract as the rest of
    this module.
    """
    try:
        params = {"modules": "price,summaryDetail,defaultKeyStatistics,financialData"}
        crumb = await _get_yf_crumb(client)
        if crumb:
            params["crumb"] = crumb
        r = await client.get(
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
            params=params,
            headers=YF_HEADERS,
            timeout=8.0,
        )
        if r.status_code == 401 and crumb:
            # Crumb may have gone stale (session cookie expired) — refetch
            # once and retry rather than giving up on every symbol for the
            # rest of the process lifetime.
            _yf_crumb_cache.pop("crumb", None)
            crumb = await _get_yf_crumb(client)
            if crumb:
                params["crumb"] = crumb
                r = await client.get(
                    f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
                    params=params,
                    headers=YF_HEADERS,
                    timeout=8.0,
                )
        if r.status_code != 200:
            return None
        result = (r.json().get("quoteSummary", {}).get("result") or [None])[0]
        if not result:
            return None

        def raw(module: str, field: str):
            v = (result.get(module, {}) or {}).get(field, {})
            return v.get("raw") if isinstance(v, dict) else None

        price = result.get("price", {}) or {}
        out = {
            "symbol": symbol,
            "name": price.get("longName") or price.get("shortName") or symbol,
            "currency": price.get("currency"),
            "price": raw("price", "regularMarketPrice"),
            "changePct": raw("price", "regularMarketChangePercent"),
            "marketCap": raw("price", "marketCap"),
            "peRatio": raw("summaryDetail", "trailingPE") or raw("defaultKeyStatistics", "trailingPE"),
            "forwardPE": raw("summaryDetail", "forwardPE"),
            "priceToBook": raw("defaultKeyStatistics", "priceToBook"),
            "debtToEquity": raw("financialData", "debtToEquity"),
            "returnOnEquityPct": (
                raw("financialData", "returnOnEquity") * 100
                if raw("financialData", "returnOnEquity") is not None else None
            ),
            "profitMarginPct": (
                raw("financialData", "profitMargins") * 100
                if raw("financialData", "profitMargins") is not None else None
            ),
            "revenueGrowthPct": (
                raw("financialData", "revenueGrowth") * 100
                if raw("financialData", "revenueGrowth") is not None else None
            ),
            "earningsGrowthPct": (
                raw("financialData", "earningsGrowth") * 100
                if raw("financialData", "earningsGrowth") is not None else None
            ),
            "epsTTM": raw("defaultKeyStatistics", "trailingEps"),
            "dividendYieldPct": (
                raw("summaryDetail", "dividendYield") * 100
                if raw("summaryDetail", "dividendYield") is not None else None
            ),
            "fiftyTwoWeekHigh": raw("summaryDetail", "fiftyTwoWeekHigh"),
            "fiftyTwoWeekLow": raw("summaryDetail", "fiftyTwoWeekLow"),
            "totalCashPerShare": raw("financialData", "totalCashPerShare"),
        }
        # Require at least price + one fundamental field, otherwise this
        # symbol adds no value over a plain price quote and should be
        # treated as a miss so callers can skip it.
        has_fundamental = any(
            out[k] is not None for k in ("peRatio", "debtToEquity", "returnOnEquityPct", "marketCap")
        )
        if out["price"] is None or not has_fundamental:
            return None
        return out
    except Exception as e:
        log.debug("yf_quote_summary failed for %s: %s", symbol, e)
        return None


async def fetch_stock_fundamentals(company_names: list[str]) -> list[dict]:
    """Resolves each free-text company name to a ticker and pulls verified
    valuation/debt/profitability fundamentals for it. Best-effort per name —
    a company that fails to resolve or has no fundamentals data is simply
    omitted, never faked. Caps at 6 names so a noisy extraction upstream
    can't turn into a slow report generation call.
    """
    out: list[dict] = []
    seen_symbols: set[str] = set()
    async with httpx.AsyncClient() as client:
        for name in company_names[:6]:
            match = await _yf_symbol_search(client, name)
            if not match or match["symbol"] in seen_symbols:
                continue
            data = await _yf_quote_summary(client, match["symbol"])
            if data:
                seen_symbols.add(match["symbol"])
                out.append(data)
    log.info("market_data: resolved fundamentals for %d/%d company name(s)", len(out), len(company_names))
    return out


def format_stock_fundamentals_as_source(stocks: list[dict]) -> Optional[dict]:
    """Wraps fetched per-company fundamentals as a synthetic, high-trust
    source dict, same shape/contract as format_quotes_as_source() above."""
    if not stocks:
        return None
    lines = []
    for s in stocks:
        parts = [f"{s['name']} ({s['symbol']})"]
        if s.get("price") is not None:
            chg = f" ({s['changePct']:+.2f}%)" if s.get("changePct") is not None else ""
            parts.append(f"price {s['price']:.2f} {s.get('currency') or ''}{chg}".strip())
        if s.get("marketCap") is not None:
            parts.append(f"market cap {s['marketCap']:,.0f}")
        if s.get("peRatio") is not None:
            parts.append(f"trailing P/E {s['peRatio']:.2f}x")
        if s.get("forwardPE") is not None:
            parts.append(f"forward P/E {s['forwardPE']:.2f}x")
        if s.get("priceToBook") is not None:
            parts.append(f"P/B {s['priceToBook']:.2f}x")
        if s.get("debtToEquity") is not None:
            parts.append(f"debt-to-equity {s['debtToEquity']:.2f}")
        if s.get("returnOnEquityPct") is not None:
            parts.append(f"ROE {s['returnOnEquityPct']:.2f}%")
        if s.get("profitMarginPct") is not None:
            parts.append(f"net margin {s['profitMarginPct']:.2f}%")
        if s.get("revenueGrowthPct") is not None:
            parts.append(f"revenue growth (YoY) {s['revenueGrowthPct']:.2f}%")
        if s.get("earningsGrowthPct") is not None:
            parts.append(f"earnings growth (YoY) {s['earningsGrowthPct']:.2f}%")
        if s.get("epsTTM") is not None:
            parts.append(f"EPS (TTM) {s['epsTTM']:.2f}")
        if s.get("dividendYieldPct") is not None:
            parts.append(f"dividend yield {s['dividendYieldPct']:.2f}%")
        if s.get("fiftyTwoWeekHigh") is not None and s.get("fiftyTwoWeekLow") is not None:
            parts.append(f"52-week range {s['fiftyTwoWeekLow']:.2f}-{s['fiftyTwoWeekHigh']:.2f}")
        lines.append(" | ".join(parts))
    return {
        "title": (
            "VERIFIED LIVE STOCK FUNDAMENTALS (authoritative, sourced directly from exchange "
            "data — use these exact figures for P/E, market cap, debt-to-equity, ROE, margins, "
            "growth rates, EPS, dividend yield, and 52-week range for these companies; do not "
            "substitute a vaguer 'typical'/'historical range' figure from another source when a "
            "verified number is listed here for the same company and metric)"
        ),
        "url": "internal://verified-stock-fundamentals",
        "snippet": " || ".join(lines)[:2000],
        "fullContent": "\n".join(lines),
    }


async def fetch_historical_stock_quotes(
    symbols: list[dict], range_str: str = "2y", interval: str = "3mo",
) -> list[dict]:
    """Same idea as fetch_historical_index_quotes() but for individually
    resolved company tickers, e.g. symbols=[{"symbol": "RELIANCE.NS", "name":
    "Reliance Industries"}]. Returns a real closing-price series per company
    so the report can build an actual price-trend line chart instead of
    guessing at a trajectory.
    """
    out: list[dict] = []
    async with httpx.AsyncClient() as client:
        for s in symbols[:6]:
            points = await _yf_chart_history(client, s["symbol"], range_str, interval)
            if points and len(points) >= 2:
                out.append({"label": s.get("name") or s["symbol"], "points": points})
    return out


def format_quotes_as_source(quotes: list[dict]) -> Optional[dict]:
    """Wraps fetched quotes as a synthetic, high-trust "source" dict in the
    same shape as Tavily results, so it can be prepended to the sources list
    that feeds the report-generation prompt."""
    if not quotes:
        return None
    lines = [
        f"{q['label']}: {q['price']:.2f} ({q['changePct']:+.2f}%)"
        for q in quotes
    ]
    return {
        "title": "VERIFIED LIVE INDEX DATA (authoritative — use these exact figures for any index price/level, do not use other sources for these numbers)",
        "url": "internal://verified-index-quotes",
        "snippet": "; ".join(lines),
        "fullContent": "\n".join(lines),
    }
