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
    ("^cnxmid", "^CNXMID", "NIFTY MIDCAP"),
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
