"""
Screener.in fundamentals knowledge base client.

Reads the Postgres/Supabase schema populated by load_screener_data.py
(see backend/screener_kb/schema.sql and backend/screener_kb/load_screener_data.py)
via Supabase's PostgREST API — the same access pattern already used
elsewhere in this backend (utils/keys.py, routes/chat.py _sb_headers).

Public entry point:
    await get_company_context(user_message)  -> str | None

Returns a compact, LLM-ready markdown context block for the single
company (if any) that the user's message appears to be about, pulling
live from the `companies` / `ratios` / `financials` / `growth_cagr` /
`shareholding` / `peers` / `pros_cons` / `documents` tables. Returns
None when no company can be confidently resolved, so callers can fall
back to web search / general knowledge as before.

Falls back to SUPABASE_URL / SUPABASE_ANON_KEY (already set for the
rest of the app) unless SCREENER_SUPABASE_URL / SCREENER_SUPABASE_ANON_KEY
are set, which lets the knowledge base live in a separate Supabase
project from sessions/Paperly if desired.
"""
import logging
import os
import re
import time
from typing import Optional

import httpx

log = logging.getLogger("screener_kb")

_SUPABASE_URL = (
    os.environ.get("SCREENER_SUPABASE_URL")
    or os.environ.get("SUPABASE_URL", "")
).rstrip("/")
_SUPABASE_KEY = (
    os.environ.get("SCREENER_SUPABASE_ANON_KEY")
    or os.environ.get("SUPABASE_ANON_KEY", "")
)

_REST = f"{_SUPABASE_URL}/rest/v1" if _SUPABASE_URL else ""


def _headers(extra: Optional[dict] = None) -> dict:
    h = {
        "apikey": _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def kb_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


# ─── Company name/ticker resolution ─────────────────────────────────────────
# We keep an in-memory {ticker, name} index refreshed periodically, rather
# than round-tripping to Postgres on every chat message, and match it
# against free-text user prompts with a lightweight heuristic (no NLP
# dependency). This mirrors how classify_query()/needs_web_search() in
# chat.py are deterministic/regex-based rather than model calls.

_INDEX_TTL_SECONDS = 6 * 60 * 60  # 6h — company roster changes rarely
_index: list[dict] = []           # [{id, ticker, name, _norm_name, _name_tokens}]
_index_loaded_at: float = 0.0

_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|inc|incorporated|corp|corporation|co|company|plc|"
    r"industries|industry|enterprises|holdings|group)\.?\s*$",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Tokens too generic to trust alone as a company-name match (avoids e.g.
# matching "IT" or "power" or "national" inside an unrelated sentence).
_GENERIC_TOKENS = {
    "india", "indian", "national", "general", "global", "international",
    "industries", "industry", "corporation", "company", "limited", "ltd",
    "bank", "finance", "financial", "financials", "power", "energy",
    "steel", "motors", "auto", "pharma", "health", "healthcare", "tech",
    "technologies", "technology", "systems", "solutions", "services",
    "group", "holdings", "enterprises", "capital", "insurance", "cement",
    "textiles", "chemicals", "and", "the", "of", "for",
}


def _normalize(name: str) -> str:
    n = name.lower().strip()
    n = _SUFFIX_RE.sub("", n).strip()
    n = _NON_ALNUM_RE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


async def _refresh_index(client: httpx.AsyncClient) -> None:
    global _index, _index_loaded_at
    rows: list[dict] = []
    offset = 0
    page_size = 1000
    while True:
        resp = await client.get(
            f"{_REST}/companies",
            headers=_headers(),
            params={
                "select": "id,ticker,name",
                "order": "id.asc",
                "limit": str(page_size),
                "offset": str(offset),
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
        if offset > 20000:  # sanity cap
            break

    built = []
    for r in rows:
        name = r.get("name") or ""
        norm = _normalize(name)
        tokens = set(_WORD_RE.findall(norm))
        built.append({
            "id": r["id"],
            "ticker": (r.get("ticker") or "").upper(),
            "name": name,
            "_norm_name": norm,
            "_tokens": tokens,
        })
    _index = built
    _index_loaded_at = time.time()
    log.info("screener_kb: loaded %d companies into resolver index", len(_index))


async def _ensure_index(client: httpx.AsyncClient) -> None:
    if not _index or (time.time() - _index_loaded_at) > _INDEX_TTL_SECONDS:
        try:
            await _refresh_index(client)
        except Exception as exc:
            log.warning("screener_kb: index refresh failed: %s", exc)


def _resolve_from_index(query: str) -> Optional[dict]:
    """Best-effort match of a free-text query against the company index.

    Strategy, cheapest/most-confident first:
      1. Exact ticker mention as a standalone word (e.g. "TCS", "RELIANCE").
      2. Full normalized company name appears verbatim in the query.
      3. A distinctive (non-generic) multi-word or long single-word chunk
         of the company name appears in the query.
    Returns None rather than guessing when nothing clears the bar — a
    missed lookup just falls back to web search, a wrong one poisons the
    answer with the wrong company's financials.
    """
    if not query or not _index:
        return None
    q_lower = query.lower()
    q_words = set(_WORD_RE.findall(q_lower))

    # 1. Ticker match — whole word, case-insensitive
    for c in _index:
        if c["ticker"] and c["ticker"].lower() in q_words:
            return c

    # 2. Full normalized name substring match (handles "reliance industries")
    best = None
    best_len = 0
    for c in _index:
        if c["_norm_name"] and len(c["_norm_name"]) > 3 and c["_norm_name"] in q_lower:
            if len(c["_norm_name"]) > best_len:
                best, best_len = c, len(c["_norm_name"])
    if best:
        return best

    # 3. Distinctive token overlap — require at least one non-generic,
    # length>=4 token shared, and require it to appear as a whole word.
    candidates = []
    for c in _index:
        distinctive = {t for t in c["_tokens"] if t not in _GENERIC_TOKENS and len(t) >= 4}
        hit = distinctive & q_words
        if hit:
            candidates.append((len(hit), c))
    if len(candidates) == 1:
        return candidates[0][1]
    # Ambiguous (0 or 2+ equally-generic matches) — don't guess.
    return None


# ─── Report/research-engine integration ─────────────────────────────────────
# Same free-text → company resolution as get_company_context(), but (a) takes
# a list of candidate name strings already extracted by the caller (see
# routes/report.py _extract_company_candidates) instead of resolving from a
# raw message, and (b) returns full multi-period snapshots formatted for
# charts/tables rather than a single condensed chat-context block, since a
# report's whole point is plotting a trend across periods, not just stating
# the latest one.

async def resolve_companies(names: list[str], limit: int = 6) -> list[dict]:
    """Resolve a list of free-text candidate names to companies in the KB.
    Best-effort per name — skips names that don't resolve or resolve
    ambiguously; dedupes by company id."""
    if not kb_configured() or not names:
        return []
    try:
        async with httpx.AsyncClient() as client:
            await _ensure_index(client)
    except Exception as exc:
        log.warning("screener_kb: resolve_companies index refresh failed: %s", exc)
        return []
    out = []
    seen_ids = set()
    for name in names[:limit]:
        match = _resolve_from_index(name)
        if match and match["id"] not in seen_ids:
            seen_ids.add(match["id"])
            out.append(match)
    return out


async def fetch_screener_fundamentals(company_names: list[str], limit: int = 6) -> list[dict]:
    """Resolves each free-text company name to a KB company and pulls its
    full multi-period snapshot (ratios, quarterly/annual financials, growth
    CAGR, shareholding history, peers, pros/cons). Best-effort — a company
    that fails to resolve or fetch is simply omitted, never faked."""
    if not kb_configured() or not company_names:
        return []
    matches = await resolve_companies(company_names, limit=limit)
    if not matches:
        return []
    try:
        async with httpx.AsyncClient() as client:
            snaps = await _gather(*(_fetch_snapshot(client, m) for m in matches))
        log.info("screener_kb: resolved %d/%d company name(s) to full snapshots",
                  len(snaps), len(company_names))
        return list(snaps)
    except Exception as exc:
        log.warning("screener_kb: fetch_screener_fundamentals failed: %s", exc)
        return []


def _series_for_chart(rows: list[dict], statement: str, line_item: str, n: int = 8) -> list[tuple[str, str]]:
    """Returns up to n (period, value) pairs, oldest-first (chart-ready order),
    for one line item of one statement."""
    matches = [r for r in rows if r.get("statement") == statement and r.get("line_item") == line_item]
    matches = matches[:n]  # rows arrive period.desc from the query — most recent n
    matches = list(reversed(matches))  # oldest-first for a left-to-right trend line
    return [(r["period"], _fmt_num(r.get("value"))) for r in matches]


def format_screener_snapshots_as_source(snapshots: list[dict]) -> Optional[dict]:
    """Wraps one or more full company snapshots as a single synthetic,
    high-trust source dict — same {title, url, snippet, fullContent} shape
    used by utils/market_data.py's format_*_as_source() helpers, so it
    drops straight into routes/report.py's `sources` list. Deliberately
    lays out each metric as an explicit period-by-period series (not just
    the latest value) so the report-writing model can lift the numbers
    directly into a multi-point chart or table instead of only ever
    plotting a single snapshot bar.
    """
    if not snapshots:
        return None

    blocks = []
    for snap in snapshots:
        c = snap.get("company") or {}
        name = c.get("name", "Unknown")
        ticker = c.get("ticker", "?")
        fin = snap.get("financials") or []
        lines = [f"{name} ({ticker}):"]

        if c.get("current_price") is not None:
            lines.append(f"  CMP ₹{_fmt_num(c['current_price'])}"
                         + (f", Market Cap ₹{_fmt_num(c['market_cap'])} Cr" if c.get("market_cap") is not None else "")
                         + (f", P/E {_fmt_num(c['pe_ratio'])}" if c.get("pe_ratio") is not None else ""))

        ratios = snap.get("ratios") or []
        if ratios:
            r_bits = [f"{r['metric']}: {r.get('raw_value') or _fmt_num(r.get('value'))}"
                      for r in ratios[:15] if r.get("metric")]
            if r_bits:
                lines.append("  Key ratios — " + " | ".join(r_bits))

        for statement, items in (
            ("Quarterly", ["Sales", "Net Profit", "Operating Profit", "EPS in Rs"]),
            ("Annual P&L", ["Sales", "Net Profit", "Operating Profit", "EPS in Rs"]),
            ("Balance Sheet", ["Total Assets", "Total Liabilities", "Borrowings", "Reserves"]),
            ("Cash Flow", ["Cash from Operating Activity", "Cash from Investing Activity", "Cash from Financing Activity"]),
        ):
            for item in items:
                series = _series_for_chart(fin, statement, item, n=8)
                if len(series) >= 2:  # a single point isn't a trend worth charting
                    series_str = ", ".join(f"{p}: {v}" for p, v in series)
                    lines.append(f"  {statement} — {item} by period (oldest→latest): {series_str}")

        growth = snap.get("growth_cagr") or []
        if growth:
            g_bits = [f"{g['metric']} ({g.get('period', '')}): {g.get('raw_value') or _fmt_num(g.get('value'))}"
                      for g in growth if g.get("metric")]
            if g_bits:
                lines.append("  Growth CAGR — " + " | ".join(g_bits))

        sh = snap.get("shareholding") or []
        if sh:
            by_holder: dict[str, list[dict]] = {}
            for r in sh:
                by_holder.setdefault(r["holder_type"], []).append(r)
            for holder, rows in by_holder.items():
                series = list(reversed(rows[:8]))
                if len(series) >= 2:
                    series_str = ", ".join(f"{r['period']}: {r.get('raw_value') or _fmt_num(r.get('value'))}" for r in series)
                    lines.append(f"  Shareholding — {holder} by period (oldest→latest): {series_str}")

        peers = snap.get("peers") or []
        if peers:
            for p in peers[:8]:
                metrics = p.get("metrics") or {}
                metric_str = ", ".join(f"{k}: {v}" for k, v in list(metrics.items())[:6])
                lines.append(f"  Peer — {p.get('peer_name', '?')}: {metric_str}")

        pc = snap.get("pros_cons") or []
        if pc:
            pros = [p["point"] for p in pc if p.get("kind") == "Pros"]
            cons = [p["point"] for p in pc if p.get("kind") == "Cons"]
            if pros:
                lines.append("  Pros: " + "; ".join(pros[:6]))
            if cons:
                lines.append("  Cons: " + "; ".join(cons[:6]))

        blocks.append("\n".join(lines))

    full = "\n\n".join(blocks)
    return {
        "title": (
            "VERIFIED SCREENER.IN FUNDAMENTALS DATABASE (authoritative, multi-period — "
            "every period-by-period series listed here is real filed data, not an estimate. "
            "Use these exact numbers, in this exact period order, to build charts/tables for "
            "quarterly/annual Sales, Net Profit, EPS, balance sheet, cash flow, growth CAGR, "
            "and shareholding trends — do not invent additional periods beyond what's listed, "
            "and do not substitute a vaguer figure from another source when a verified number "
            "is listed here for the same company/metric/period)"
        ),
        "url": "internal://verified-screener-fundamentals",
        "snippet": full[:2000],
        "fullContent": full,
    }


# ─── Fetching a company's full snapshot ─────────────────────────────────────


async def _get(client: httpx.AsyncClient, table: str, company_id: int, **params) -> list[dict]:
    q = {"company_id": f"eq.{company_id}", **params}
    resp = await client.get(f"{_REST}/{table}", headers=_headers(), params=q, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


async def _fetch_snapshot(client: httpx.AsyncClient, company: dict) -> dict:
    cid = company["id"]
    company_row_resp = await client.get(
        f"{_REST}/companies",
        headers=_headers(),
        params={"id": f"eq.{cid}", "select": "*"},
        timeout=10.0,
    )
    company_row_resp.raise_for_status()
    company_rows = company_row_resp.json()
    company_row = company_rows[0] if company_rows else company

    ratios, financials, growth, shareholding, peers, pros_cons = await _gather(
        _get(client, "ratios", cid, order="metric.asc"),
        _get(client, "financials", cid, order="statement.asc,period.desc"),
        _get(client, "growth_cagr", cid, order="metric.asc"),
        _get(client, "shareholding", cid, order="period.desc"),
        _get(client, "peers", cid, order="row_num.asc", limit="8"),
        _get(client, "pros_cons", cid, order="row_num.asc"),
    )
    return {
        "company": company_row,
        "ratios": ratios,
        "financials": financials,
        "growth_cagr": growth,
        "shareholding": shareholding,
        "peers": peers,
        "pros_cons": pros_cons,
    }


async def _gather(*coros):
    import asyncio
    return await asyncio.gather(*coros, return_exceptions=False)


# ─── Formatting the snapshot into an LLM context block ──────────────────────

def _fmt_num(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 1e7:
        return f"{f / 1e7:.2f} Cr"
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}"


def _latest_periods(rows: list[dict], statement: str, line_items: list[str], n: int = 4) -> list[str]:
    by_item: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("statement") != statement:
            continue
        by_item.setdefault(r["line_item"], []).append(r)
    lines = []
    for item in line_items:
        item_rows = by_item.get(item)
        if not item_rows:
            continue
        # rows already ordered period.desc from the query
        top = item_rows[:n]
        vals = ", ".join(f"{r['period']}: {_fmt_num(r.get('value'))}" for r in top)
        lines.append(f"  - {item}: {vals}")
    return lines


def format_snapshot_as_context(snap: dict) -> str:
    c = snap["company"]
    lines = [f"### {c.get('name', 'Unknown')} ({c.get('ticker', '?')}) — Screener.in fundamentals database"]

    price_bits = []
    if c.get("current_price") is not None:
        price_bits.append(f"CMP ₹{_fmt_num(c['current_price'])}")
    if c.get("price_change_pct") is not None:
        price_bits.append(f"{_fmt_num(c['price_change_pct'])}% change")
    if c.get("price_date_label"):
        price_bits.append(f"as of {c['price_date_label']}")
    if c.get("market_cap") is not None:
        price_bits.append(f"Market Cap ₹{_fmt_num(c['market_cap'])} Cr")
    if c.get("pe_ratio") is not None:
        price_bits.append(f"P/E {_fmt_num(c['pe_ratio'])}")
    if price_bits:
        lines.append("**Snapshot:** " + " · ".join(price_bits))
    if c.get("website"):
        lines.append(f"Website: {c['website']}")

    ratios = snap.get("ratios") or []
    if ratios:
        lines.append("\n**Key ratios:**")
        for r in ratios[:20]:
            if r.get("metric"):
                lines.append(f"  - {r['metric']}: {r.get('raw_value') or _fmt_num(r.get('value'))}")

    growth = snap.get("growth_cagr") or []
    if growth:
        lines.append("\n**Growth (CAGR):**")
        for g in growth:
            if g.get("metric"):
                lines.append(f"  - {g['metric']} ({g.get('period', '')}): {g.get('raw_value') or _fmt_num(g.get('value'))}")

    fin = snap.get("financials") or []
    if fin:
        for statement, items in (
            ("Quarterly", ["Sales", "Net Profit", "Operating Profit", "EPS in Rs"]),
            ("Annual P&L", ["Sales", "Net Profit", "Operating Profit", "EPS in Rs"]),
            ("Balance Sheet", ["Total Assets", "Total Liabilities", "Borrowings", "Reserves"]),
            ("Cash Flow", ["Cash from Operating Activity", "Cash from Investing Activity", "Cash from Financing Activity"]),
        ):
            block = _latest_periods(fin, statement, items, n=4)
            if block:
                lines.append(f"\n**{statement} (most recent periods):**")
                lines.extend(block)

    sh = snap.get("shareholding") or []
    if sh:
        latest_period = sh[0].get("period") if sh else None
        latest_rows = [r for r in sh if r.get("period") == latest_period]
        if latest_rows:
            lines.append(f"\n**Shareholding pattern ({latest_period}):**")
            for r in latest_rows:
                lines.append(f"  - {r['holder_type']}: {r.get('raw_value') or _fmt_num(r.get('value'))}")

    peers = snap.get("peers") or []
    if peers:
        lines.append("\n**Peer comparison:**")
        for p in peers[:8]:
            metrics = p.get("metrics") or {}
            metric_str = ", ".join(f"{k}: {v}" for k, v in list(metrics.items())[:6])
            lines.append(f"  - {p.get('peer_name', '?')} — {metric_str}")

    pc = snap.get("pros_cons") or []
    if pc:
        pros = [p["point"] for p in pc if p.get("kind") == "Pros"]
        cons = [p["point"] for p in pc if p.get("kind") == "Cons"]
        if pros:
            lines.append("\n**Pros:** " + "; ".join(pros[:6]))
        if cons:
            lines.append("**Cons:** " + "; ".join(cons[:6]))

    return "\n".join(lines)


# ─── Public entry point ─────────────────────────────────────────────────────

async def get_company_context(user_message: str) -> Optional[str]:
    """Resolve a company from free text and return a formatted fundamentals
    context block, or None if the KB isn't configured, no company can be
    confidently resolved, or the lookup fails for any reason."""
    ctx, _meta = await get_company_context_with_meta(user_message)
    return ctx


async def get_company_context_with_meta(user_message: str) -> tuple[Optional[str], Optional[dict]]:
    """Same resolution as get_company_context(), but also returns a small
    {id, ticker, name} dict identifying the matched company so callers
    (routes/chat.py) can point the user at a downloadable source-of-truth
    export (see build_snapshot_workbook / GET /api/stocks/{id}/export.xlsx)
    for the exact data used to answer. Returns (None, None) on no match."""
    if not kb_configured() or not user_message:
        return None, None
    try:
        async with httpx.AsyncClient() as client:
            await _ensure_index(client)
            match = _resolve_from_index(user_message)
            if not match:
                return None, None
            snap = await _fetch_snapshot(client, match)
            ctx = format_snapshot_as_context(snap)
            c = snap.get("company") or match
            meta = {
                "id": match["id"],
                "ticker": c.get("ticker") or match.get("ticker"),
                "name": c.get("name") or match.get("name"),
            }
            return ctx, meta
    except Exception as exc:
        log.warning("screener_kb: get_company_context_with_meta failed: %s", exc)
        return None, None


# ─── Excel export — the exact KB data as a downloadable "source of truth" ───
# Rebuilds a workbook shaped like the original Screener.in export (one sheet
# per table) directly from what's stored in Postgres, so a user can verify
# any number the chatbot quoted against the underlying rows.

def build_snapshot_workbook(snap: dict) -> bytes:
    from io import BytesIO
    from openpyxl import Workbook

    c = snap.get("company") or {}
    wb = Workbook()
    _first_sheet_used = {"done": False}

    def sheet(name):
        if not _first_sheet_used["done"]:
            _first_sheet_used["done"] = True
            ws = wb.active
            ws.title = name[:31]
            return ws
        return wb.create_sheet(name[:31])

    # Company_Info
    ws = sheet("Company_Info")
    info_fields = [
        ("Ticker", c.get("ticker")), ("Company Name", c.get("name")),
        ("Company URL", c.get("company_url")), ("Website", c.get("website")),
        ("Consolidated", c.get("consolidated")), ("Current Price", c.get("current_price")),
        ("Price Change %", c.get("price_change_pct")), ("Price Date", c.get("price_date_label")),
        ("Market Cap", c.get("market_cap")), ("Stock P/E", c.get("pe_ratio")),
    ]
    ws.append([f for f, _ in info_fields])
    ws.append([v for _, v in info_fields])

    # Top_Ratios
    ws = sheet("Top_Ratios")
    ws.append(["Metric", "Value", "Numeric Value"])
    for r in snap.get("ratios") or []:
        ws.append([r.get("metric"), r.get("raw_value"), r.get("value")])

    # Financials — one sheet per statement, wide (line item x period)
    fin = snap.get("financials") or []
    by_statement: dict[str, dict[str, dict[str, str]]] = {}
    periods_by_statement: dict[str, list[str]] = {}
    for row in fin:
        st, period, item = row.get("statement"), row.get("period"), row.get("line_item")
        if not st or not item:
            continue
        by_statement.setdefault(st, {}).setdefault(item, {})[period] = row.get("raw_value") or row.get("value")
        plist = periods_by_statement.setdefault(st, [])
        if period not in plist:
            plist.append(period)
    sheet_name_for = {
        "Quarterly": "Quarterly Results", "Annual P&L": "Profit & Loss_1",
        "Balance Sheet": "Balance Sheet", "Cash Flow": "Cash Flow",
        "Annual Ratios": "Ratios",
    }
    for statement, items in by_statement.items():
        periods = sorted(periods_by_statement.get(statement, []))
        ws = sheet(sheet_name_for.get(statement, statement)[:31])
        ws.append(["Line Item"] + periods)
        for item, values in items.items():
            ws.append([item] + [values.get(p) for p in periods])

    # Growth_CAGR
    ws = sheet("Growth_CAGR")
    ws.append(["Metric", "Period", "Value", "Numeric Value"])
    for g in snap.get("growth_cagr") or []:
        ws.append([g.get("metric"), g.get("period"), g.get("raw_value"), g.get("value")])

    # Shareholding — split by frequency, wide (holder x period)
    sh = snap.get("shareholding") or []
    for freq, sheet_name in (("Quarterly", "Shareholding_Quarterly"), ("Yearly", "Shareholding_Yearly")):
        rows = [r for r in sh if r.get("frequency") == freq]
        if not rows:
            continue
        by_holder: dict[str, dict[str, str]] = {}
        periods: list[str] = []
        for r in rows:
            by_holder.setdefault(r["holder_type"], {})[r["period"]] = r.get("raw_value") or r.get("value")
            if r["period"] not in periods:
                periods.append(r["period"])
        periods = sorted(periods)
        ws = sheet(sheet_name)
        ws.append(["Holder"] + periods)
        for holder, values in by_holder.items():
            ws.append([holder] + [values.get(p) for p in periods])

    # Peers
    peers = snap.get("peers") or []
    if peers:
        ws = sheet("Peers")
        cols: list[str] = []
        for p in peers:
            for k in (p.get("metrics") or {}).keys():
                if k not in cols:
                    cols.append(k)
        ws.append(["Name"] + cols)
        for p in peers:
            metrics = p.get("metrics") or {}
            ws.append([p.get("peer_name")] + [metrics.get(k) for k in cols])

    # Pros_Cons
    pc = snap.get("pros_cons") or []
    if pc:
        ws = sheet("Pros_Cons")
        ws.append(["Type", "Point"])
        for p in pc:
            ws.append([p.get("kind"), p.get("point")])

    # Documents
    docs = snap.get("documents") or []
    if docs:
        ws = sheet("Documents")
        ws.append(["Document", "URL"])
        for d in docs:
            ws.append([d.get("title"), d.get("url")])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def search_companies(query: str, limit: int = 10) -> list[dict]:
    """Explicit search-by-name/ticker for API endpoints (not free-text
    resolution) — used by routes/stocks.py."""
    if not kb_configured() or not query:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_REST}/companies",
                headers=_headers(),
                params={
                    "select": "id,ticker,name,current_price,price_change_pct,market_cap,pe_ratio",
                    "or": f"(ticker.ilike.*{query}*,name.ilike.*{query}*)",
                    "limit": str(limit),
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        log.warning("screener_kb: search_companies failed: %s", exc)
        return []


async def get_company_snapshot_by_id(company_id: int) -> Optional[dict]:
    if not kb_configured():
        return None
    try:
        async with httpx.AsyncClient() as client:
            return await _fetch_snapshot(client, {"id": company_id})
    except Exception as exc:
        log.warning("screener_kb: get_company_snapshot_by_id failed: %s", exc)
        return None
