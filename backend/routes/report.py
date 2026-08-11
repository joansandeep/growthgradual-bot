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
from utils.datawrapper import attach_datawrapper_charts
from utils.market_data import (
    fetch_index_quotes, format_quotes_as_source,
    fetch_historical_index_quotes, format_historical_quotes_as_source,
)

router = APIRouter()
log = logging.getLogger("report")

# ── OKF context fetcher ────────────────────────────────────────────────────────
import os as _os
_SB_URL = _os.environ.get("SUPABASE_URL", "").rstrip("/")
_SB_KEY = _os.environ.get("SUPABASE_ANON_KEY", "")
_OKF_BUCKET = "paperly-uploads"
_OKF_PREFIX = "paperly-okf"


def _indian_fy_quarter(d):
    """Return (label, quarter_start, quarter_end) for date `d` under the
    Indian fiscal year (April-March), e.g. July 2026 -> ('Q2 FY27', 2026-07-01, 2026-09-30).

    This exists because leaving fiscal-quarter arithmetic to the LLM produces
    inconsistent/wrong labels (seen: "Q2 FY27" used for two different
    quarters in the same report, and for a period that should have been
    Q1 FY27). Computing it here and handing the model the exact label removes
    that arithmetic from its job entirely.
    """
    from datetime import date
    import calendar
    if d.month >= 4:
        fy_end_year = d.year + 1
        month_in_fy = d.month - 3  # Apr=1 .. Dec=9
    else:
        fy_end_year = d.year
        month_in_fy = d.month + 9  # Jan=10 .. Mar=12
    q_num = (month_in_fy - 1) // 3 + 1  # 1..4
    fy_label = f"FY{str(fy_end_year)[-2:]}"
    q_start_month_in_fy = (q_num - 1) * 3 + 1
    q_start_month = q_start_month_in_fy + 3 if q_start_month_in_fy <= 9 else q_start_month_in_fy - 9
    q_start_year = fy_end_year - 1 if q_start_month >= 4 else fy_end_year
    q_start = date(q_start_year, q_start_month, 1)
    q_end_month = q_start_month + 2
    q_end_year = q_start_year
    if q_end_month > 12:
        q_end_month -= 12
        q_end_year += 1
    q_end = date(q_end_year, q_end_month, calendar.monthrange(q_end_year, q_end_month)[1])
    return f"Q{q_num} {fy_label}", q_start, q_end


def _past_n_quarters_anchor(question: str, today) -> str:
    """
    If the question references "past/last N quarters" (or "8 quarters" etc.),
    pre-compute the exact Indian-FY quarter window and hand the model a
    literal list of quarter labels with their calendar date ranges, instead
    of relying on it to derive fiscal-quarter labels from the current date
    itself -- that arithmetic is where mislabeled/inconsistent quarters (e.g.
    "Q2 FY27" reused for two different periods) have come from.
    Returns "" if the question has no such reference.
    """
    m = re.search(r"\b(?:past|last)\s+(\d{1,2})\s+quarters?\b", question.lower())
    if not m:
        return ""
    n = int(m.group(1))

    from datetime import timedelta
    cur_label, cur_start, cur_end = _indian_fy_quarter(today)
    # The current quarter is still in progress unless today is its last day;
    # "past N quarters" of completed data should end at the last FULLY
    # completed quarter, not the one still running.
    if today < cur_end:
        last_complete_end = cur_start - timedelta(days=1)
    else:
        last_complete_end = cur_end

    labels = []
    probe = last_complete_end
    for _ in range(n):
        label, q_start, q_end = _indian_fy_quarter(probe)
        labels.append((label, q_start, q_end))
        probe = q_start - timedelta(days=1)
    labels.reverse()

    lines = "\n".join(
        f"  - {label}: {q_start.strftime('%b %d, %Y')} - {q_end.strftime('%b %d, %Y')}"
        for label, q_start, q_end in labels
    )
    return (
        f"\n\nQUARTER WINDOW (pre-computed -- use these exact labels and dates, do NOT "
        f"recompute fiscal quarters yourself, and do not use any other quarter label "
        f"anywhere in the report):\n{lines}\n"
        f"The current quarter ({cur_label}, {cur_start.strftime('%b %d, %Y')} - "
        f"{cur_end.strftime('%b %d, %Y')}) is still in progress as of {today.strftime('%B %d, %Y')} "
        f"and is NOT part of the {n}-quarter window above unless it is already the last "
        f"row shown.\n"
    )


def _normalize_filename(s: str) -> str:
    """Lowercase + strip punctuation/whitespace for fuzzy filename matching
    between OKF frontmatter titles and the raw filenames sent by the client
    (which may differ slightly in spacing/quoting)."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


async def _fetch_okf_context(session_id: str, max_chars: int = 12000,
                              current_filenames: set[str] | None = None) -> str:
    """Fetch OKF concept files for a session from Supabase Storage and return
    them as a single structured context block for the report LLM prompt.

    Each concept file is markdown with YAML frontmatter (type, title,
    description, tags, timestamp) followed by the extracted document text.
    This gives the LLM a structured, typed view of each uploaded file —
    much more grounded than raw FAISS chunk text.

    current_filenames: if provided and non-empty, ONLY documents whose OKF
    title matches one of these (normalized, fuzzy substring match) are
    included. This matters because session_id is a long-lived client-side ID
    that can persist across many separate, unrelated file uploads over days —
    without this filter, a document uploaded in an earlier, unrelated session
    (e.g. a fund factsheet) keeps silently riding along into every later
    report for that same browser session (e.g. one about a completely
    different company), showing up as a spurious extra "source" and giving
    the model irrelevant context to potentially blend in. If current_filenames
    is empty (no file freshly attached this turn — e.g. a pure RAG follow-up
    question), no filtering is applied and the full session context is used,
    since there's no signal to scope by and that's the existing intended
    behaviour for follow-up questions about previously uploaded documents.

    Returns an empty string if Supabase isn't configured or no OKF bundle
    exists for this session — the existing RAG/file-context path still runs.
    """
    if not _SB_URL or not _SB_KEY or not session_id:
        return ""

    norm_current = {_normalize_filename(n) for n in (current_filenames or set())}

    headers = {"apikey": _SB_KEY, "Authorization": f"Bearer {_SB_KEY}"}
    prefix = f"{_OKF_PREFIX}/{session_id}"
    concept_texts: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # List all documents in the session's OKF folder
            list_resp = await client.post(
                f"{_SB_URL}/storage/v1/object/list/{_OKF_BUCKET}",
                headers={**headers, "Content-Type": "application/json"},
                json={"prefix": f"{prefix}/documents", "limit": 50},
            )
            if not list_resp.is_success:
                log.debug("OKF list failed %d: %s", list_resp.status_code, list_resp.text[:80])
                return ""

            entries = list_resp.json()
            if not entries:
                return ""

            # Fetch each concept file and strip YAML frontmatter — keep only
            # the human-readable body for the LLM (frontmatter is for machines)
            total = 0
            for entry in entries[:20]:  # cap at 20 docs
                name = entry.get("name", "")
                if not name or not name.endswith(".md"):
                    continue
                obj_resp = await client.get(
                    f"{_SB_URL}/storage/v1/object/{_OKF_BUCKET}/{prefix}/documents/{name}",
                    headers=headers,
                )
                if not obj_resp.is_success:
                    continue
                md = obj_resp.text

                # Strip YAML frontmatter (--- ... ---) and keep body
                body = re.sub(r'^---\n.*?\n---\n', '', md, flags=re.DOTALL).strip()

                # Extract title from frontmatter for labelling
                title_match = re.search(r'^title:\s*"?([^"\n]+)"?', md, re.MULTILINE)
                doc_title = title_match.group(1).strip() if title_match else name.replace(".md", "")

                if norm_current:
                    norm_title = _normalize_filename(doc_title)
                    # Fuzzy substring match either direction — OKF titles and
                    # client filenames can differ slightly in truncation/slugging.
                    if not any(nc in norm_title or norm_title in nc for nc in norm_current):
                        continue  # stale document from an earlier, unrelated upload — skip it

                chunk = f"### Source: {doc_title}\n\n{body}"
                remaining = max_chars - total
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining] + "\n\n[…truncated]"
                concept_texts.append(chunk)
                total += len(chunk)

    except Exception as exc:
        log.warning("OKF fetch error: %s", exc)
        return ""

    return "\n\n---\n\n".join(concept_texts)

SKIP_PAGE_FETCH = [".pdf", "bloomberg.com", "wsj.com", "ft.com", "economist.com", "investing.com"]

SYSTEM_PROMPT = """You are a senior research analyst at Growth Gradual. Write a COMPREHENSIVE, well-structured research report on ANY topic using the scraped web sources provided.
You MUST respond with valid JSON only — no markdown fences, no preamble, no text outside JSON.

Respond with EXACTLY this shape:
{
  "title": "<concise NOUN-PHRASE report title, max 12 words. Examples: 'Top Banking Stocks India 2026', 'Indian Mutual Fund SIP Returns Analysis', 'HDFC vs ICICI Bank Comparison', 'Nifty 50 Market Outlook — June 2026'. STRICT RULES: NEVER start with 'So', 'You', 'I', 'Let's', 'Here', 'Based', 'Looking', 'Understanding', 'A look at', 'An analysis of', or any verb/pronoun. NEVER start with 'The' followed by a verb — e.g. BAD: 'The Nifty outlook today is mixed, with some analysts predicting...' (full sentence starting with The) → GOOD: 'Nifty 50 Outlook — Mixed Signals Amid Volatility'. NEVER write a full sentence — this includes sentences that don't start with one of those words too, e.g. BAD: 'The Minimum Investment Amounts For These Top-Performing SIPs Are As Follows' → GOOD: 'Top-Performing SIP Funds — Minimum Investment Requirements'. Never end a title with a colon, 'as follows', or '...' — those signal an incomplete sentence, not a heading. ALWAYS write a noun phrase — topic first, qualifiers after.>",
  "report": "<full markdown report — target 3500-4500 words (MINIMUM 22000 characters — shorter responses will be rejected and retried), structured and data-rich. This is a LONG-FORM report (aim for ~12-15 printed pages once charts/tables/images are laid in) — see REPORT STRUCTURE below for the section list that gets you there. Add DEPTH, not invented data: more context, more explanation of mechanisms and causes, more comparison and discussion of what the numbers mean — never pad with filler sentences or numbers not in the sources.>",
  "charts": [...],
  "images": [{ "prompt": "<AI image-generation prompt — see AI IMAGE RULES>", "caption": "<short caption>" }, ...] (1-2 items — see AI IMAGE RULES, currently requires at least 1),
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
✓ The longer length target below is a DEPTH requirement, not a license to pad: hit it by explaining
  mechanisms, context, comparisons, and implications more thoroughly — never by inventing extra figures.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHART RULES — READ CAREFULLY. CHARTS ARE MANDATORY WHERE DATA EXISTS.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TABLE vs CHART — THEY MUST NOT SHOW THE SAME NUMBERS TWICE.
A table and a chart placed in the same subsection must each earn their place by
showing something the other doesn't — never the identical set of labels/values
rendered once as rows and once as bars right below. Decide per subsection:
  • Data has ≥4 columns of mixed types (e.g. AMC name, approach, category, AUM,
    several return periods) → TABLE ONLY. A chart can't hold that many
    dimensions cleanly, and forcing one just repeats the table in picture form.
  • Data is a single ranked metric across several named items (e.g. "1-year
    return by strategy", "sector performance %") → CHART ONLY. Don't also dump
    the same ranked list into a table right next to it — the chart already
    shows every label and value clearly.
  • If you genuinely need both (e.g. a compact chart for the visual takeaway
    plus a fuller table with extra columns the chart omits), the table must
    add at least one column/detail the chart does NOT show — otherwise drop
    the table and keep only the chart, or vice versa.
  • Vary which subsections get a table vs a chart vs points-only (bullet list)
    — a report where every subsection is "table, then bar chart of the exact
    same rows" reads as repetitive and mechanical, not professional. Rotate
    formats deliberately across 3.1–3.4 so the report alternates rhythm.

NARRATED LISTS → TABLES, NOT PARAGRAPHS: any time you are about to describe 3+ comparable
items that share the same attributes — a sequence of dated events, several entities each with
a metric, a set of policy changes each with an effective date — stop and render it as a markdown
table with named columns (e.g. Date | Event | Impact, or Entity | Metric | Change) instead of
narrating each one as its own sentence inside a paragraph. A paragraph that reads "On June 5 the
RBI held rates, then on June 11 the US struck Iran, then on June 19 Accenture cut guidance..." is
exactly the pattern to avoid — that is a table wearing prose. Reserve paragraph prose for genuine
analysis and connective reasoning between data points, not for listing the data points themselves.

NEVER DRAW DIAGRAMS OUT OF TEXT CHARACTERS: do not represent a funnel, flowchart, pipeline, or any
other multi-step sequence using bracketed stage names and box/arrow symbols on their own lines (e.g.
"[Stage One]" / "■" / "▼" / "→" stacked as a pseudo-diagram). This renders as literal, broken-looking
characters in the PDF, not an actual diagram — there is no rendering support for ASCII/unicode art in
the report body. Any step-by-step sequence (a funnel, a process, a pipeline) must instead become
either: (a) a numbered markdown list with a short bolded stage name and 1-2 sentences per step (this
is the default — use it for most sequences), or (b) an arrow chart if the sequence is really a
before-vs-after metric change for named items, or (c) a simple table with columns like Stage | What
Happens | Output. Whichever you pick, it also counts toward the STEP 4 chart/table floor above.

STEP 1 — AGGRESSIVELY SCAN sources for ANY chartable numbers:
  • Returns/performance of multiple funds, stocks, sectors → bar chart
  • Rankings with numbers (top 5 SIPs by return, top gainers) → bar chart
  • Time-series: quarterly results, monthly data, weekly prices → line chart
  • Allocation/composition (sector weights, portfolio mix) → pie chart
  • Comparisons: 1yr vs 3yr vs 5yr returns of same fund → bar chart
  • A metric that CHANGED FROM ONE VALUE TO ANOTHER for the same named items —
    guidance revised from X% to Y%, price target raised from ₹A to ₹B, a rating
    upgraded/downgraded, before-vs-after any number — → ARROW CHART, not a bar
    chart. This is one of the most common shapes in an earnings/guidance story
    ("cut FY26 guidance from +1-3% to flat-to-down 1%") and reads far better as
    an arrow from the old value to the new one than as two separate bars.
    → {"type":"arrow","series":[{"name":"Previous","data":[{"label":"Fiserv","value":2}]},
                                  {"name":"Revised","data":[{"label":"Fiserv","value":-1}]}]}
    → Needs ≥2 named items with a real before/after pair each; for a single item,
      state the before/after in text instead (an arrow chart needs ≥2 items same as bar/pie).
  • Two INDEPENDENT numeric metrics given for the same set of named entities where
    the RELATIONSHIP between them is the actual point (e.g. valuation score vs.
    1-year return, P/E vs. revenue growth, market cap vs. daily % move) → SCATTER
    chart, not two separate bar charts. Use this whenever the report's own prose
    is making a "despite X being high, Y is low" / "X correlates with Y" argument —
    that argument IS the scatter plot.
    → {"type":"scatter","series":[{"name":"1-Yr Return %","data":[{"label":"Astera Labs","value":86.3}]},
                                    {"name":"Valuation Score /6","data":[{"label":"Astera Labs","value":0}]}]}
    → Needs ≥4 named entities with BOTH metrics present — fewer than that, a scatter
      plot is just a few dots with nothing to show a pattern; use a table instead.
  • FII/DII flows by date → line chart if 3+ dates given. For a single-day FII vs
    DII comparison (one net-buy figure, one net-sell figure — opposite signs),
    use a 2-bar chart, NOT a pie: a pie slice can't represent a negative outflow,
    so plotting FII (e.g. -735 Cr) against DII (e.g. +705 Cr) as pie shares
    produces a meaningless percentage. Only use pie for genuinely non-negative
    share-of-whole splits (e.g. green vs red IPO listings, both counts ≥0).
  • Category-wise RETURNS (large cap vs mid cap vs small cap performance) → bar chart
  • Category-wise AUM/asset totals (how much money sits in each PMS/fund category,
    sector, or AMC out of the whole) → this is composition, not ranking — use a
    PIE chart, even though the report already has bar charts of top performers by
    return. Sum AUM per category first (skip UNDISC./blank entries), then chart
    only categories with a real total. Don't skip this just because bar charts
    were already used elsewhere in the report — composition data gets a pie
    chart on its own merits, for chart-type variety.

  SINGLE-ASSET TREND OVER TIME — DON'T SKIP THIS ONE:
  Whenever the sources give the level of ONE index/stock/rate at several dates or
  sessions (e.g. "Nifty 50 closed at X on Jun 1, Y on Jun 11, Z on Jun 19..."),
  that is a single-series LINE chart — this is one of the most valuable charts in
  a markets report and is frequently under-used. Use the actual session/event dates
  as labels (not just start/end) so the line shows real path, not just two points.
  → Example: "Nifty 50 — June 2026 Session Levels" → line, series=[{"name":"Nifty 50",
     "data":[{"label":"Jun 1","value":23654},{"label":"Jun 11","value":23167},
             {"label":"Jun 19","value":24013},{"label":"Jun 30","value":23866}]}]
  → Do this for ANY index/benchmark/rate the sources track across multiple dated
    points, not only when the question explicitly asks for a "trend" or "chart".
  → NEVER build a line chart from just 2 known values (e.g. "Q4 2025: 880.18" and
    "Q1 2026: 880.52") even if you're tempted to space them across a date axis —
    a straight line across many gridlines between 2 points implies daily/weekly
    data that doesn't exist. With only 2 real values, use a bar chart instead (or
    state the before/after figures in text), not a line.
  → NEVER plot different TYPES of % change (previous session %, past-month %,
    past-year %, etc.) as if they were sequential points on a timeline — they
    describe different, non-chronological periods, and a line through them draws
    a fake trend. Use a bar chart with each period as its own labeled bar instead
    (e.g. "Sensex — % Change By Period": bars for "1 Day", "1 Month", "1 Year").

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

  COMPOSITION / BREAKDOWN TOPICS — USE A STACKED BAR WHEN IT FITS:
  When a label (e.g. a quarter, a fund, a sector) breaks down into 2+ parts of
  a whole — portfolio allocation by asset class per fund, revenue split by
  segment per quarter, expense breakdown by category — use a multi-series bar
  chart with each part as its own series sharing the same labels, AND set
  "stacked": true on the chart spec. This renders as a true stacked column
  chart (segments stacked bottom-to-top, one column per label) — leaving
  "stacked" unset/false on a multi-series bar instead renders side-by-side
  GROUPED bars, which is the right shape for entity-vs-entity comparisons but
  wrong for a composition/breakdown.
  → Example: "Portfolio Allocation by Fund" → bar, stacked=true,
     series: [{"name":"Equity","data":[{"label":"Fund A","value":65},{"label":"Fund B","value":40}]},
              {"name":"Debt","data":[{"label":"Fund A","value":25},{"label":"Fund B","value":45}]},
              {"name":"Cash","data":[{"label":"Fund A","value":10},{"label":"Fund B","value":15}]}]
  → Only do this when the parts genuinely sum to the whole for each label (e.g. ~100% allocation,
     or segments of one total revenue figure) — don't force unrelated metrics into a stacked shape.
  → A single pie chart still works fine for ONE label's breakdown; reach for the stacked-bar
     shape above once you're comparing that breakdown ACROSS multiple labels (funds/quarters/sectors).


  • SIP topic → bar chart of top 5 funds by 3-yr return %
  • Stock topic → bar chart of key financial metrics (revenue, profit growth %)
  • Banking topic → bar chart of NIM/NPA/ROE across banks
  • Market topic → bar chart of sector performance %
  • Mutual fund topic → pie chart of category allocation OR bar of returns by category

STEP 2 — ONLY create a chart if ALL conditions are met:
  ✓ At least 3 data points (bar/pie) or 4 time points (line), 2 items (arrow), 4 items (scatter)
  ✓ All labels are DIFFERENT from each other
  ✓ All values are DIFFERENT from each other (not all the same)
  ✓ Values come from the source data — do NOT invent numbers
  ✗ NEVER create a chart from a single number
  ✗ NEVER duplicate labels
  ✗ NEVER use future/projected values you invented
  ✗ A bar chart needs ≥3 named distinct items; a pie chart needs ≥2; an arrow chart
    needs ≥2; a scatter chart needs ≥4 — this is enforced server-side and anything
    short of that WILL be silently dropped, wasting the slot.
  → If the topic naturally centers on 2 entities (e.g. "HDFC vs ICICI"), actively
    scan the rest of the sources for OTHER comparable entities mentioned anywhere
    (peer banks, sector averages, other funds in the same category, etc.) and add
    them as additional bars/slices so the chart clears the ≥3-item bar. If no
    third comparable entity exists anywhere in the sources, skip the chart
    entirely rather than rendering a thin 2-item one.
  → PARTIAL DATA: If some entities in a comparison lack a specific metric (e.g. SBI
    market cap not found), use only the entities that HAVE that data. A chart with
    3 entities that have real data is better than 5 entities where 2 have made-up values.
    NEVER invent chart values. NEVER use 0 as a placeholder — omit the entity instead.
  → CHART VARIETY: when a topic has multiple metrics (e.g. banking has market cap,
    P/E, NIM, NPA, ROE), create DIFFERENT charts for different metrics rather than
    repeating the same entities with the same data. E.g. Chart 1: Market Cap bar chart
    for banks WITH market cap data; Chart 2: P/E ratio bar chart for banks WITH P/E data;
    Chart 3: Net Profit bar chart. Each chart stands alone.

  AGGREGATE-ONLY DATA — DON'T FORCE A CHART, USE A TABLE INSTEAD:
  When sources name multiple entities together but only give ONE combined/aggregate
  figure for all of them (e.g. "IndiGo, Vedanta, and Whirlpool saw combined promoter
  sales of Rs.12,000 crore" — three companies, one number), that is NOT a 3-item bar
  chart — it's a single data point wearing three names. Charting it will be silently
  dropped server-side (need ≥3 DISTINCT values, not ≥3 names sharing one value), wasting
  a chart slot you could have used elsewhere. Instead:
  → FIRST, actively re-scan the sources for each entity's INDIVIDUAL figure — company-specific
    press coverage often gives per-entity numbers even when a summary sentence combines them.
    If you find 3+ individual values, THAT is your real bar chart.
  → If individual figures genuinely aren't in the sources, do NOT chart it at all — present
    it instead as a markdown table (e.g. columns: Company/Entity | Transaction Type | Value |
    Period | Primary Beneficiary), using "Significant"/"Substantial" for the value column where
    only the combined figure is known. Tables render as a visual in the final PDF just like
    charts do, so this still adds to your 5-8 visual target — it just isn't a fabricated chart.
  → This applies any time you catch yourself about to write a chart series where every entity
    would share the exact same value — that's the tell that it's one aggregate number, not
    real per-item data.

STEP 3 — Place [CHART_n] inline in the report markdown right after the paragraph AND bullet list whose data it shows.
  charts[0] = [CHART_1], charts[1] = [CHART_2], etc.
  IMPORTANT: Never place [CHART_n] immediately after just 1-2 sentences — always ensure at least one full
  paragraph (3+ sentences) or a paragraph + bullet list precedes the chart. This prevents blank whitespace
  gaps in the PDF.

STEP 4 — MINIMUM 6 charts/tables per report, no exceptions unless sources are genuinely numeric-free.
  This report runs long (10-12 pages), and visuals — not walls of text — are what fill that length
  well and make the report interesting to read. Target 7-10 total when the sources support it; treat
  6 as the floor, not an aspiration. BEFORE FINALIZING, DO THIS COUNT EXPLICITLY: add up the total
  number of entries across BOTH your "charts" array AND every markdown table you wrote in the report
  body — that combined number, not just the charts array alone, is what must be ≥6. If the combined
  total is under 6, go back through the sources/file data and STEP 1's per-scenario list again — there
  is almost always another chartable angle you skipped (a ratio, a trend, a breakdown, a comparison
  across a different pairing of the same entities) rather than genuinely no more data. Only report
  fewer than 6 if the sources are so thin there is truly nothing left to chart — that should be rare,
  not the default outcome. Vary the shapes (bar, stacked bar, line, pie, arrow, scatter) rather than
  repeating the same shape for every chart; use the stacked-bar shape above whenever a breakdown
  is compared across multiple labels.

  THIN CHARTS COUNT AGAINST YOU, NOT FOR YOU: a bar/pie chart needs ≥3 distinct labels on its
  category axis, and this applies to grouped/multi-series bar charts too — "2 groups × 3 series each"
  is still only 2 category-axis labels and reads as sparse, not as 6 data points. If a comparison
  naturally has only 2 anchor points (e.g. "current state" vs "target state" for the same set of
  metrics), do NOT force it into a single grouped bar chart — instead either (a) split it into one
  small chart per metric where each has ≥3 meaningful labels, (b) use an arrow chart per metric
  (Previous → Target, one arrow per named metric = multiple items, not one 2-bar group), or (c) drop
  the chart and present it as a table, which still counts toward the STEP 4 floor above.

  TABLES SHOULD OFTEN CARRY A COMPANION CHART, NOT STAND ALONE: per the TABLE vs CHART rule above,
  whenever a table's data has one ranked/comparable column that would read clearly as a visual on its
  own, pull that column out into its own compact chart alongside the fuller table — this is one of the
  easiest ways to hit the chart-count floor above without padding, since the table already did the
  data-gathering work. Don't force this when the table's rows genuinely don't reduce to one clean
  chartable column — but default to looking for that opportunity rather than leaving every table
  chart-less.

  CHART-TYPE VARIETY IS MANDATORY, NOT OPTIONAL: bar charts are the easiest shape to reach for,
  which is exactly why reports drift into making EVERY chart a bar chart even when the underlying
  data fits a different shape much better. Before finalizing the "images"/"charts" list, check the
  full set you've built: if the report has 4+ charts and 3 or more of them are plain (non-stacked,
  non-grouped) bar charts, go back through STEP 1's per-scenario rules above and actively look for
  data you may have force-fit into a bar chart that actually belongs as an arrow chart (any before/
  after or revised-guidance number), a scatter chart (any two-metrics-per-entity relationship), a
  line/area chart (any single trend across 4+ points), or a pie/donut (any composition-of-a-whole).
  A report is not required to use every type, but repeating the exact same bar-chart shape for most
  of the report's visuals is a sign the data was matched to the easiest chart, not the right one.

Chart spec shape:
{
  "type": "bar" | "line" | "pie" | "arrow" | "scatter",
  "title": "<specific title e.g. 'Top 5 SIP Funds — 3-Year Returns' not 'Chart 1'>",
  "unit": "%" | "₹" | "Cr" | "B" | "$" | "x" | "",
  "xLabel": "<what the x-axis categories are, e.g. 'Fund' or 'Sector' or 'Session Date'>",
  "yLabel": "<what the y-axis values represent, e.g. '3-Yr Return (%)' or 'Index Level'>",
  "stacked": true | false,  // ONLY for multi-series bar charts where each series is
                            // a PART of a whole per label (e.g. Equity/Debt/Cash per
                            // fund) — set true so it renders as one stacked column
                            // per label instead of side-by-side grouped bars. Omit
                            // or set false for comparison charts (entity vs entity).
  "series": [{ "name": "<series name>", "data": [{ "label": "<unique label>", "value": <number> }] }]
}
"arrow" and "scatter" both use the SAME two-series-sharing-labels shape as any other
multi-series chart (see COMPARISON TOPICS above) — no extra fields needed:
  → arrow: series = [{"name":"Previous","data":[...]}, {"name":"Revised"/"Current","data":[...]}]
  → scatter: series = [{"name":"<x-metric>","data":[...]}, {"name":"<y-metric>","data":[...]}]
AXIS LABELS ARE MANDATORY for every bar/line/arrow/scatter chart — always fill in "xLabel" and
"yLabel" with a short (1-4 word) description of what each axis represents. A chart with numeric
tick marks but no axis title leaves the reader guessing what the numbers mean — never omit these
two fields.

PERIOD FRAMING: whenever the underlying data is a month-to-date or year-to-date figure, say so
explicitly in the chart title (e.g. "Nifty 50 — MTD Performance", "Sectoral Returns, YTD") rather
than a generic title — this matters as much for line charts tracking an index/stock across sessions
as for bar charts comparing entities, since the reader needs to know the time window at a glance.

GOOD chart examples — do exactly this:
  • "Top SIP Funds by 3-Yr Return" → bar, labels=[ICICI Pru Value, Nippon India Value, UTI Gold ETF, Quant Small Cap], values=[15.9, 15.8, 35.2, 28.4], unit="%"
  • "Sectoral PAT Growth Q4FY26" → bar, labels=[Utilities, Metals, Retail, Healthcare, BFSI], values=[61,53,32,32,18], unit="%"  
  • "Nifty 50 Quarterly EPS" → line, labels=[Q1FY25,Q2FY25,Q3FY25,Q4FY25,Q1FY26], values=[actual numbers from source]
  • "Top Banking Stocks — ROE %" → bar, labels=[HDFC Bank, ICICI Bank, Kotak, SBI, Axis], values from source
  • "MF Category Inflows" → pie, labels=[Large Cap, Mid Cap, Small Cap, Flexi Cap, ELSS], values=₹ crore
  • "Portfolio Allocation by Fund" → bar with "stacked":true (multi-series, parts of a whole per label — see above)

BAD chart examples — NEVER do this:
  ✗ labels=["Today","Today","Today"] — duplicate labels
  ✗ values=[100, 100, 100] — identical values  
  ✗ Inventing numbers not in sources
  ✗ Line chart with only 2 data points
  ✗ [CHART_n] in report without matching charts[n-1] entry

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AI IMAGE RULES — DATA NEVER GOES IN AN IMAGE. [TEMP: ALWAYS INCLUDE AT LEAST 1 — being trialed]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Every number, ranking, trend, or comparison belongs in a [CHART_n] or a table — NEVER in a
generated image. Images here are AI-generated (via Gemini), not stock/web photography, and exist
to give the report a visual anchor — a conceptual/editorial illustration, never a substitute for a chart.
✓ TEMPORARY: every report MUST include at least 1 image (2 is fine too) — pick the single most
  visual/scene-like moment in the report (a place, an event, an industry, a process) even if every
  section already has a chart or table. This overrides any "images are rare/optional" instinct —
  for now, "images": [] is only acceptable if the topic is so abstract there is truly no scene to
  depict (e.g. "explain the yield curve inversion formula").
✓ Maximum 2 images per report. Never one per subsection, never "for visual variety."
✓ Each entry: {"prompt": "<scene description for an image generator>", "caption": "<1 short sentence>"}.
✓ WRITE A SPECIFIC, CONCRETE SCENE — not a generic mood board. The prompt must name an actual
  subject tied to THIS report's content: a specific industry setting, a specific activity, a specific
  vantage point/angle, specific lighting, specific composition. Think like an art director briefing a
  photographer for a named publication, not like someone typing "business concept" into a stock site.
  ✗ BANNED CLICHÉS — never generate any of these regardless of topic, they are the generic-stock-photo
  defaults every image model reaches for and they add zero visual interest: a laptop open on a wooden
  desk with a coffee cup/plant/notebook; a handshake in a blurred office; a generic city skyline at
  sunset; a magnifying glass over a chart; stacks of gold coins/rising coin towers; a lightbulb icon;
  a rocket launching; people pointing at a whiteboard; a generic "team meeting" around a table.
  ✓ INSTEAD go specific and sensory: name the actual place/process/object from the report (a specific
  kind of workshop floor, a specific market stall, a specific type of machinery, a specific texture or
  material, a specific weather/time-of-day), and specify an unusual but natural camera angle (low angle,
  overhead, through a doorway, close-up on hands doing the actual work) — e.g. instead of "laptop on a
  desk," write "overhead shot of a small workshop table cluttered with fabric swatches and a sewing
  machine mid-stitch, warm afternoon light through a window, shallow depth of field" if the report is
  about a boutique tailoring business.
  Style: describe it as an editorial illustration or a documentary-style photograph (pick whichever
  suits the topic), with a concrete color/lighting direction (e.g. "muted navy and warm gold tones,
  soft directional light" or "high-contrast documentary photography, natural light") — never leave the
  visual style to chance.
  Never ask for text, numbers, charts, logos, tickers, or any real named/branded company mark to appear
  IN the image; the model generating the picture cannot render accurate data or trademarks, so asking
  for them produces misleading or unusable output.
✓ Reference each with [WEB_IMG_n] inline in the report body, at the point in the section it illustrates
  — same placeholder mechanics as [CHART_n], numbered in the order the "images" array lists them.
✗ Never invent a caption number/stat not already stated elsewhere in the report as its own bullet/table/chart.
✓ FINAL SELF-CHECK before submitting each image prompt — read your own prompt back and ask: "Could this
  exact sentence describe literally any office/industry, or does it name something only THIS report's
  topic would produce?" A prompt like "a modern office with a desk, monitor, and whiteboard" or "a
  minimalist meeting room with a screen" passes visually as fine but is still a reskinned version of the
  banned clichés — generic room + generic tech + generic furniture, with no detail that ties it to the
  report's actual subject. If your prompt would still make sense with the company/industry name swapped
  out for a random unrelated one, rewrite it: name the specific artifact, material, tool, or activity from
  THIS report (the specific deliverable being built, the specific niche being sold to, the specific
  document/dashboard/product on screen — described generically, never as real branded UI) before
  finalizing.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPORT STRUCTURE — PLAN THE SECTIONS YOURSELF, EVERY TIME, FROM THE ACTUAL QUESTION:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
There is no fixed template. Before writing, decide the section list this specific report needs —
driven by the question asked and what the sources actually contain, not by habit or by what last
report used. Two reports on different questions must read as if a different analyst planned each
one from scratch, down to the section names.

REQUIRED ANCHORS (always present, but shape freely within them):
  1. A title (# heading).
  2. An "Executive Summary" section (must contain the literal words "Executive Summary" as a heading)
     — a desk-note: 1 short paragraph (3-4 sentences) on the single biggest story, then a
     "Key Takeaways" bullet list (5-7 items, each leading with a concrete number/%/level — never a
     vague statement). No methodology talk, no source list here.
  3. A "Risks & Considerations" (or equivalently-named risk/caveats section) — 3-5 distinct risks,
     each a **bold label** + 2-3 sentences, grounded in the sources or reasoned from the data patterns.
  4. A closing synthesis section (Conclusion/Outlook/whatever name fits) — 1-2 short paragraphs +
     a "Key Takeaways" or "What To Watch" bullet list.
  5. A "Data Sources" section — one markdown table (Publication | Data type) and nothing else.
     List every distinct publication that contributed a real fact — not just the most-cited one.
     Do NOT include a URL/link column — the table names the publication and what it contributed,
     nothing more; raw URLs never appear anywhere in the report, in this table or elsewhere.
     This is the ONLY sources listing — no second copy anywhere else in the report.

EVERYTHING BETWEEN Executive Summary and Risks & Considerations IS YOURS TO DESIGN:
  → Pick 3-6 body sections (with subsections where useful) that map onto the REAL angles this
    question and these sources support. Name them for the actual topic — e.g. a single-stock
    question might use "Financial Performance", "Valuation vs Peers", "Analyst Views"; a sector
    question might use "Sub-Sector Breakdown", "Policy Backdrop", "Key Players"; a market-moves
    question might use "Index Performance", "Sector Rotation", "What Moved The Market". An
    "Introduction" and generic "Data Analysis / 3.1, 3.2..." numbering are ONE possible shape, not
    the default — use them only if they genuinely fit better than a topic-specific structure.
  → Section count follows the sources: 2 sections when only 2 angles have real data, 5 when 5 do.
    A thin section padded to hit a count is worse than a shorter, denser report.
  → Optionally open with a short (1-2 paragraph, 100-150 word) framing/context section before the
    numbered findings if the topic needs background a first-time reader wouldn't have — skip it
    entirely for a narrow, self-explanatory question.
  → Optionally include a "Key Findings" style section (8-12 bold-stat-led one-liners, each with a
    number) if the material suits a scannable findings list — fold it into the body sections instead
    if that reads better for this particular topic.

FORMAT RATIO — STRUCTURED CONTENT LEADS, PARAGRAPHS SUPPORT:
  Across the whole report, points/tables/charts should carry MORE of the informational weight than
  narrative paragraphs do. Concretely, for every body section:
  → Open with AT MOST 1-2 short paragraphs of framing/analysis (aim ~60-100 words) — never 3-4.
  → Then represent the actual data as a bullet list, a markdown table, or a [CHART_n] — pick per the
    TABLE vs CHART rule above — not buried inside more paragraph sentences.
  → Any time you're about to describe 3+ comparable items in prose, stop and make it a table or
    bullet list instead (see NARRATED LISTS rule above).
  → A paragraph earns its place only for genuine connective reasoning (why X caused Y, what the
    combination of two data points implies) — never for restating numbers a table/chart/bullet
    already shows.
  → Vary which format leads section to section (table here, chart there, bullets elsewhere) so nothing
    reads mechanical.
  → Treat each section as its own small piece of design, not a repeat of the last one's shape. Across
    the report as a whole, deliberately rotate through EVERY available element — markdown tables,
    [CHART_n] bar/line/pie/donut charts, bullet and numbered lists, blockquote callouts (see PULL-QUOTES
    below), and [WEB_IMG_n]/AI-generated images (see Images below) — so a reader flipping through feels
    like each section was laid out on purpose. Two sections in a row leaning on the exact same shape
    (e.g. "paragraph then bullet list" twice back to back) is the failure mode to avoid; two sections in
    a row each pairing a different pair of elements (chart + callout, then table + image) is the goal.
  Minimum word counts are gone — a section that says everything it needs in 120 words of framing +
  a table + a bullet list is complete. Depth comes from adding another real, source-grounded
  bullet/row/chart-series, not from writing longer sentences around the same facts.

GLOBAL RULES:
- NO REPETITION ACROSS SECTIONS: each specific stat, comparison, or finding is stated FULLY once,
  in the single section it belongs to most, and referenced only in passing elsewhere (e.g. "as noted
  above, Nifty Bank's 6.4% gain..."). Before writing a new sentence, check whether the same number or
  claim already appeared earlier in the report — if so, either cut it or shorten it to a brief callback,
  never restate it at full length again. This applies especially to the Key Takeaways bullets, the Key
  Findings section, and the Conclusion, which commonly drift into re-explaining the same 2-3 headline
  stats already covered in the Executive Summary — each of those sections must surface DIFFERENT facts,
  not reformulations of the same ones.
- SIGNED NUMBERS FOR GAINS/LOSSES: every percentage change, delta, or gain/loss figure — in prose,
  bullets, tables, AND chart data — must be written with an explicit leading "+" for positive values
  and "-" for negative values (e.g. "+6.4%", "-9.6%", never a bare "6.4%" for a change figure or "(-9.6%)").
  This sign is load-bearing: the PDF renderer colors these green/red based on the leading character, so an
  unsigned number renders in neutral ink and loses the visual cue entirely. Absolute levels that aren't a
  change (e.g. an index closing level, a P/E ratio) do not need a sign.
- FORMATTING DENSITY — MANDATORY: no section may run more than 2 consecutive paragraphs without
  a structural break — a bullet list, a table, or a chart. EVERY body section needs its OWN bullet
  list, table, or chart — "the report has bullets somewhere" does not satisfy a specific section's
  requirement, each one earns its own. If you catch yourself writing a 3rd paragraph in a row with
  no bullets/table/chart between, stop and convert part of it into a bullet list instead — break out
  specific numbers, named entities, or ranked items as list items rather than narrating them inside
  a sentence.
- ACCURACY MANDATE: every number, date, name, and statistic in the report must trace verbatim to a
  specific value found in the sources — never invented, never estimated, never "rounded for
  readability" away from the source's actual figure. If you are not certain a number appears in the
  sources as written, omit that claim entirely rather than approximating it. Before closing the
  report, mentally re-scan every figure you wrote and confirm each one matches a source value
  exactly — this matters more than hitting the word-count target.
- NO BACKGROUND-KNOWLEDGE FILL-IN: if the subject is a real, named company/fund/entity you recognise,
  you may already "know" plausible-sounding figures for it from training — DO NOT use those. This
  applies especially to multi-year tables (e.g. a 5-year income statement) where only one or two years'
  numbers were actually extracted (from text or vision) and the rest of the row would otherwise need to
  be your own estimate of "what those numbers probably were." A table with genuinely fewer rows/columns
  of real data is correct; a complete-looking table padded with your own guess of what a real company's
  historicals likely were is a fabrication, even if the guessed numbers are individually reasonable and
  self-consistent. If a year/column's source value was not actually extracted, state "data not available
  from sources" for that cell/row or omit it — never reconstruct it from what you already know about the
  company.
- NEVER invent any number, date, name, or statistic
- NO FALSE COMPLETENESS: if the question asks for an "exhaustive"/"complete"/"all X" list of discrete
  real-world events (e.g. every promoter stake sale, every deal, every filing) and the sources are
  ordinary news-search results rather than a structured dataset (an exchange disclosure feed, a
  regulator's database, an official registry), do NOT present the list as if it were the full set.
  News search surfaces whichever events got covered, not every event that happened — treat row count
  in your table as "notable instances found in available sources," never as "all instances," and say
  so explicitly in the surrounding prose (e.g. "the sources below are the block/bulk deals that
  received press coverage, not a complete registry of every promoter transaction in the period").
  This applies even if the retrieved sources include an aggregate total (e.g. "352 deals worth ₹X" from
  a single report) — citing that aggregate is fine, but it does not license listing individual rows as
  if they summed to the aggregate unless every one of those individual rows was itself found in a source.
- NUMERIC CONSISTENCY: when the same metric appears in more than one place (a chart, a table, keyStats, and/or the prose), it MUST use the exact same figure and precision everywhere — e.g. if a source gives 6.6%, write "6.6" in the chart, the table, and the text; never round it to "7" in one spot and "6.6" in another. Copy the figure once from the source, then reuse that exact string everywhere it recurs.
- TABLE HYGIENE: every column must have real values for EVERY row. If a column would
  be "-" or empty for some rows (e.g. indices like NIFTY 50/NIFTY BANK have no market cap),
  do NOT include that column for those rows — instead, put indices and individual stocks in
  SEPARATE tables (one for index levels, one for stock market caps), or drop the column entirely
  if it doesn't apply to the row type being shown.
- TABLE COMPLETENESS: if a data row has no values for ANY column (e.g. SBI row with no market cap,
  P/E, profit data), OMIT that row entirely from the table rather than including an empty row.
  A table with 2 data rows of real data is better than 5 rows where 3 are blank.
- NO DUPLICATE TABLES: each table must appear EXACTLY ONCE in the report. Never repeat a table
  from section 3.1 in section 3.2 or later. If you need to reference the same data again,
  refer to it by name ("as shown in the table above") rather than re-rendering it.
- SECTOR PERFORMANCE TABLE: only include a time-series table (2022/2023/2024 metrics) if you
  have ACTUAL numeric values for those years from the sources. If the data is not in the sources,
  skip that table entirely rather than including a table with all empty cells.
- NEVER use bracket citation markers such as [1], [2], [1, 2], [n] anywhere in the report body. This is a hard rule — the report reads as polished analyst prose, not an academic paper with footnote numbers. If a claim needs attribution, name the source in the sentence (e.g. "Screener.in data shows...").
- NEVER wrap ANY content in a fenced code block (```...```) or draw an ASCII/text diagram with
  arrows or boxes (e.g. "[Phase 1] --> [Phase 2] --> [Phase 3]"). This report is typeset with a
  proper heading/paragraph/table/chart renderer, not a plain-text terminal — a code fence has no
  visual meaning there, and gets discarded, while raw "-->" arrows and "[bracketed]" text just show
  up as literal, ugly text in the final PDF. Represent a sequence of phases/steps as what it
  actually is: a numbered list (STEP 1, STEP 2, ...) or a short H3-per-phase breakdown with normal
  prose under each one — never as bracket-and-arrow ASCII art, and never inside triple backticks.
- NEVER cite "Tavily" as a publication or source — Tavily is an internal search tool, not a publisher. If a fact's only origin is an internal search summary rather than a named publication, state the fact without attribution rather than inventing a citation.
- Tables and charts are the DEFAULT way to present any comparable/ranked/multi-item data — target
  3+ markdown tables where the sources genuinely support them; fewer is correct for a narrower
  question with less tabular material, more is correct for a data-rich one.
- Images: 1-3 AI-generated illustrative images (see AI IMAGE RULES) — actively look for at least one
  genuine opportunity per report (a concept, place, product, process, or scene worth visualizing),
  not only as a last resort where no chart/table fits. A report with zero images should be the
  exception (a narrow, purely numeric question with nothing visual to illustrate), not the default.
- keyStats: 10-14 real metrics with values and change indicators. These power the infographic stat-card
  strips rendered throughout the PDF (cover page, plus additional strips dropped in automatically
  wherever a section turns out data-dense — the renderer decides placement from actual content, not
  a fixed "after Executive Summary" spot) — treat them as the report's visual backbone, not an
  afterthought. Pull the single most important number from EVERY major section (Introduction context
  stat, each 3.x subsection's headline number, a Risks-adjacent stat if one exists) so the strips
  actually represent the whole report rather than only the intro. Each keyStat needs: label (short,
  e.g. "NIFTY BANK"), value (e.g. "+6.41%" or "23,865.75"), and change (signed, e.g. "+6.41%") where
  applicable.
- PULL-QUOTES / INSIGHT CALLOUTS: use a markdown blockquote (a line starting with "> ") 2-4 times
  across the report — never zero, never on every subsection — to call out the single sharpest,
  most consequential insight from the section it sits in. This renders as a distinct highlighted
  callout card, not a normal paragraph, so it must earn that treatment: one tight, punchy sentence
  (not a data recap you already put in a bullet or table — a "so what", an implication, a contrarian
  read, or the one line a reader would remember). Example: "> Valuations near 24x forward earnings
  leave little room for disappointment if Q2 guidance disappoints." Place them where the section's
  argument actually turns on that insight, not evenly spaced for the sake of it.
- LENGTH TARGET: The "report" field should land in the ~22,000-38,000 character range. Responses
  shorter than 22,000 characters will be REJECTED and regenerated, so treat that floor as real — but
  hit it through MORE structured content (more table rows, more chart series, more distinct bullets,
  another genuinely-supported section) rather than through longer paragraphs. A report that hits the
  floor with dense tables/bullets and lean prose is BETTER than one that hits it with long paragraphs.
  Never pad with filler sentences, restated points, or invented figures.
- STOP CONDITION — DO NOT OVERSHOOT: once you have written the Data Sources table (the final section),
  STOP immediately. Do not add anything after it — no extra sections, no restated conclusion, no
  repeated section numbers (there is exactly one "Key Findings", one "Risks & Considerations", one
  "Conclusion", one "Data Sources" — never write a second copy of any of them under a new number).
  Do not, under any circumstances, copy or paraphrase these instructions (this SYSTEM_PROMPT) into the
  "report" string — text like a section's own formatting rules or word-count minimums must never
  appear as report content. If you find yourself running out of new, source-grounded analysis to add,
  that means the report is finished — close it out rather than continuing to generate text.
- CRITICAL: NEVER write raw JSON inside the "report" string. Charts go ONLY in the "charts" array.
  Use [CHART_1], [CHART_2] placeholders in the report text — never paste the chart JSON object itself.
- CRITICAL: NEVER use unescaped double-quote characters (") inside JSON string values. If you need to
  quote something inside the report text, use single quotes (') instead, or escape as \". A bare "
  inside the "report" value corrupts the entire JSON and causes total report failure.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JSON COMPLETION — CRITICAL: NEVER TRUNCATE THE OUTPUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You MUST output a fully valid, complete JSON object. Truncated output causes total report failure.

BEFORE you start writing: budget your tokens. The JSON wrapper (title, charts, images, keyStats,
summary) takes ~2000 tokens. The report content needs ~6000-7000 tokens. Total: ~9000 tokens.
You have 32000 output tokens available — more than enough. Do NOT rush or compress.

Rules to prevent truncation:
1. Write your planned sections in the order you laid them out (Executive Summary first, Risks and
   Data Sources last, whatever you chose in between) — do NOT skip or abbreviate one to save tokens.
2. Pace yourself: roughly midway through your planned sections you should have written roughly half
   your target character count — if not, you are on track only if it's because you're being dense
   (tables/bullets/charts) rather than thin; keep going, do NOT start compressing.
3. The "charts" array must be COMPLETE before you close the JSON. If you run low on space, write
   shorter chart titles but include ALL chart objects.
4. Always end the JSON with: "summary": "...", "keyStats": [...]} — never leave it open.
5. If a section runs thin, EXPAND it with another real bullet/table row/chart series rather than moving on.
6. NEVER end the "report" string mid-sentence. Always close with a complete conclusion paragraph,
   then close the JSON string with " and the remaining fields.
"""

# Matches stray bracket-number citation markers like "[1]", "[1, 2]", "[8]" that
# the LLM might still slip in despite the prompt rules above. Applied as a
# belt-and-suspenders cleanup pass on every return path so they can never reach
# the PDF, even on an off-policy generation.
_CITATION_MARKER_RE = re.compile(r"\s?\[\s*\d+(?:\s*,\s*\d+)*\s*\]")


_CHART_PLACEHOLDER_RE = re.compile(r"\[CHART_(\d+)\]")


def _remap_chart_placeholders(report_text: str, original_charts: list, valid_mask: list[bool]) -> str:
    """After `_is_plausible_chart` filtering drops some charts from the array,
    the [CHART_n] placeholders the LLM placed inline (numbered against the
    ORIGINAL, unfiltered charts list) point at the wrong array index — every
    placeholder after a rejected chart is off by however many charts were
    dropped before it, and trailing placeholders run off the end of the
    shrunken array entirely.

    This rewrites every [CHART_n] in report_text to the chart's new 1-based
    position in the filtered list, or removes the placeholder line entirely
    if that chart was rejected — so a rejected chart never silently shifts
    a later, valid chart into its paragraph.
    """
    # old 1-based index -> new 1-based index (or None if rejected)
    remap: dict[int, int | None] = {}
    new_pos = 0
    for old_idx, keep in enumerate(valid_mask, start=1):
        if keep:
            new_pos += 1
            remap[old_idx] = new_pos
        else:
            remap[old_idx] = None

    def _sub(m: re.Match) -> str:
        old_n = int(m.group(1))
        new_n = remap.get(old_n)
        return f"[CHART_{new_n}]" if new_n is not None else ""

    out_lines = []
    for line in report_text.split("\n"):
        replaced = _CHART_PLACEHOLDER_RE.sub(_sub, line)
        # If the whole line was just a (now-removed) placeholder, drop the
        # line rather than leaving a blank gap.
        if line.strip().startswith("[CHART_") and not replaced.strip():
            continue
        out_lines.append(replaced)
    return "\n".join(out_lines)


# --- Web-search image support (mirrors the [CHART_n] pattern above) -------

# Filename/URL fragments that almost always mean "logo / icon / ad / avatar"
# rather than an actual illustrative photo — Tavily's image search returns a
# lot of these for any finance query (publication mastheads, social icons,
# tracking pixels) and they make a report look amateurish if embedded.
_JUNK_IMAGE_HINTS = (
    "logo", "icon", "favicon", "sprite", "avatar", "placeholder", "blank.gif",
    "pixel.gif", "tracking", "1x1", "badge", "button", "social", "share-",
    ".svg",
)

# Generic queries (e.g. "Latest market news") give Tavily's image search very
# little to anchor on, and it has been observed to return completely
# off-topic entertainment images — actor headshots, movie posters, DVD
# covers — that happen to rank well for the word "news" or similar. These
# are checked against the candidate's own DESCRIPTION (not the article text),
# so this is cheap and catches the failure mode without needing real topical
# NLP. A finance/markets report should never carry a Batman DVD cover.
_OFFTOPIC_IMAGE_HINTS = (
    "movie", "film", " actor", "actress", "dvd", "blu-ray", "poster", "trailer",
    "tv series", "tv show", "television series", "celebrity", "hollywood",
    "starring", "red carpet", "album cover", "music video", "film franchise",
    "superhero", "batman", "superman", "marvel", "dc comics", "riddler", "joker",
    "animated movie", "movie poster",
)


def _filter_image_candidates(raw_images: list[dict], limit: int = 10) -> list[dict]:
    """Dedupe by URL and drop obvious logos/icons/tracking pixels before these
    ever reach the LLM prompt — cheaper and safer than trusting the model to
    catch all of them, though the prompt also tells it to skip junk-looking
    candidates as a second line of defense."""
    import urllib.parse as _urlparse
    seen: set[str] = set()
    out: list[dict] = []
    for img in raw_images:
        url = (img.get("url") or "").strip()
        if not url or url in seen:
            continue
        low = url.lower()
        if any(hint in low for hint in _JUNK_IMAGE_HINTS):
            continue
        desc_low = (img.get("description") or "").lower()
        if any(hint in desc_low for hint in _OFFTOPIC_IMAGE_HINTS):
            continue
        seen.add(url)
        try:
            domain = _urlparse.urlparse(url).netloc.replace("www.", "")
        except Exception:
            domain = ""
        out.append({
            "url": url,
            "description": (img.get("description") or "").strip()[:200],
            "domain": domain,
        })
        if len(out) >= limit:
            break
    return out


def _build_image_candidates_block(candidates: list[dict]) -> str:
    """Render the numbered candidate-image list inserted into the user prompt.
    Domain is real info derived straight from each image's own URL (not a
    guess) — it's the only honest substitute for "what page this came from"
    since Tavily's image list isn't tied back to a specific source result."""
    if not candidates:
        return "\n\nCANDIDATE IMAGES: none available for this query — return \"images\": [].\n"
    lines = ["\n\nCANDIDATE IMAGES (reference ONLY by index — never invent or alter a URL):"]
    for i, img in enumerate(candidates, start=1):
        desc = img["description"] or "(no description)"
        domain = img.get("domain") or "unknown source"
        lines.append(f"  [{i}] ({domain}) {desc}")
    block = "\n".join(lines) + "\n"
    log.info("Report: image candidate block sent to LLM:\n%s", block)
    return block


def _validate_image_selections(parsed_images: list, candidates: list[dict]) -> tuple[list[dict], list[bool]]:
    """Cross-check the model's `images` array against the candidate list it
    was actually given. Anything with a missing/out-of-range/non-numeric
    candidateIndex is dropped rather than trusted — this is the only thing
    standing between "the model echoed back a real Tavily image URL" and
    "the model hallucinated a URL that 404s in the PDF". Returns the final
    {"url","caption"} list plus a keep-mask aligned to parsed_images' original
    order, for _remap_web_image_placeholders to use."""
    final: list[dict] = []
    mask: list[bool] = []
    for entry in parsed_images or []:
        if not isinstance(entry, dict):
            mask.append(False)
            continue
        idx = entry.get("candidateIndex")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            mask.append(False)
            continue
        if idx < 1 or idx > len(candidates):
            mask.append(False)
            continue
        caption = str(entry.get("caption") or "").strip()[:160]
        candidate = candidates[idx - 1]
        final.append({"url": candidate["url"], "caption": caption or candidate["description"] or ""})
        mask.append(True)
    return final, mask


_WEB_IMG_PLACEHOLDER_RE = re.compile(r"\[WEB_IMG_(\d+)\]")


def _inject_fallback_image_placeholders(report_text: str, images: list[dict]) -> str:
    """Spread [WEB_IMG_n] placeholders across the report body for any image
    that doesn't already have one — covers both the model placing none at all,
    and the partial case where _top_up_images added image(s) beyond what the
    model itself placed a marker for. Only fills gaps; never touches markers
    that already exist. Used by the main parse path and both JSON-salvage paths."""
    if not images:
        return report_text
    existing = {int(n) for n in _WEB_IMG_PLACEHOLDER_RE.findall(report_text)}
    missing = [i for i in range(1, len(images) + 1) if i not in existing]
    if not missing:
        return report_text
    paragraphs = report_text.split("\n\n")
    n_para = len(paragraphs)
    if len(missing) == 1:
        insertion_points = [max(1, n_para // 4)]
    else:
        step = max(1, n_para // (len(missing) + 1))
        insertion_points = [step * (k + 1) for k in range(len(missing))]
    for k, para_idx in reversed(list(enumerate(insertion_points))):
        img_idx = missing[k]
        placeholder = f"\n\n[WEB_IMG_{img_idx}]\n"
        insert_at = min(para_idx, n_para - 1)
        paragraphs.insert(insert_at, placeholder)
    return "\n\n".join(paragraphs)


def _force_fallback_images(
    images: list[dict], image_candidates: list[dict], model_used: str = "?"
) -> tuple[list[dict], bool]:
    """If image selection came back empty despite usable candidates being
    offered, force the best 1-2 through rather than ship a report with zero
    photos purely because the model played it safe — or because a JSON-salvage
    path never recovered an images array at all on a truncated response.
    Candidates here already survived the junk-domain/logo filter in
    _filter_image_candidates, so a forced pick is never a raw, unfiltered URL.
    Called identically from the main parse path and both JSON-salvage paths
    below so the guarantee holds no matter which path produced the result.
    Returns (images, was_forced) — was_forced tells the caller whether to
    treat the mask as all-valid rather than reuse a stale validation mask."""
    if images or not image_candidates:
        return images, False
    ranked = sorted(image_candidates, key=lambda c: len(c.get("description") or ""), reverse=True)
    forced = ranked[:2]
    forced_images = [
        {
            "url": c["url"],
            "caption": (c.get("description") or f"Related image from {c.get('domain') or 'source'}")[:90],
        }
        for c in forced
    ]
    log.warning(
        "Report: model=%s selected 0 images from %d candidates — forcing in %d fallback "
        "image(s) (%s) so the report doesn't end up purely text/chart-only",
        model_used, len(image_candidates), len(forced_images), [c.get("domain") for c in forced],
    )
    return forced_images, True


def _top_up_images(
    images: list[dict], image_candidates: list[dict], model_used: str = "?", target: int = 2
) -> tuple[list[dict], bool]:
    """The model is asked to pick 2-4 relevant images (see IMAGE RULES) but has
    sometimes selected just 1 even with more decent candidates left unused.
    Top up to `target` from the remaining candidates rather than ship a report
    thinner on photos than what was actually available. Never touches what the
    model (or _force_fallback_images) already picked — only adds more, and
    never re-adds a URL already in the list."""
    if len(images) >= target or not image_candidates:
        return images, False
    used_urls = {img.get("url") for img in images}
    remaining = [c for c in image_candidates if c["url"] not in used_urls]
    if not remaining:
        return images, False
    ranked = sorted(remaining, key=lambda c: len(c.get("description") or ""), reverse=True)
    added = ranked[: target - len(images)]
    new_images = images + [
        {
            "url": c["url"],
            "caption": (c.get("description") or f"Related image from {c.get('domain') or 'source'}")[:90],
        }
        for c in added
    ]
    log.warning(
        "Report: model=%s selected only %d image(s) (rules ask for 2-4) — topping up with %d more "
        "from unused candidates (%s)",
        model_used, len(images), len(added), [c.get("domain") for c in added],
    )
    return new_images, True


def _remap_web_image_placeholders(report_text: str, valid_mask: list[bool]) -> str:
    """Twin of _remap_chart_placeholders for [WEB_IMG_n] — renumbers against
    the filtered images list, dropping placeholder lines for any image that
    failed validation in _validate_image_selections."""
    remap: dict[int, int | None] = {}
    new_pos = 0
    for old_idx, keep in enumerate(valid_mask, start=1):
        if keep:
            new_pos += 1
            remap[old_idx] = new_pos
        else:
            remap[old_idx] = None

    def _sub(m: re.Match) -> str:
        new_n = remap.get(int(m.group(1)))
        return f"[WEB_IMG_{new_n}]" if new_n is not None else ""

    out_lines = []
    for line in report_text.split("\n"):
        replaced = _WEB_IMG_PLACEHOLDER_RE.sub(_sub, line)
        if line.strip().startswith("[WEB_IMG_") and not replaced.strip():
            continue
        out_lines.append(replaced)
    return "\n".join(out_lines)


# ── Leaked system-prompt detector ──────────────────────────────────────────
# Root cause of the "duplicate numbered sections full of raw instructions"
# bug: the SYSTEM_PROMPT's own length mandate ("MINIMUM 18000 characters —
# shorter responses will be rejected and retried") pushes the model to keep
# emitting tokens after it has already written a complete, correctly
# numbered report. Once it runs out of real content, it has been observed
# to degenerate into literally reciting the REPORT STRUCTURE / GLOBAL RULES
# section of its own instructions back into the "report" string — restarting
# the section numbering (e.g. a second "Key Findings" as "8." right after a
# real one at "4.") and copying meta-instructions like "Each finding must
# follow this exact format" verbatim as if they were findings.
#
# These phrases only ever appear in SYSTEM_PROMPT itself — a real report
# body should never contain them. If any shows up in the model's output, the
# text should be truncated right before it: everything after a leaked
# fragment is prompt-echo, not content, and a resurfaced earlier section
# number confirms the report already logically ended before this point.
_LEAKED_PROMPT_MARKERS = (
    "each finding must follow this exact format",
    "bold lead sentence with a specific stat",
    "no bracket citation markers",
    "3-5 distinct risks",
    "minimum 200 words total",
    "if sources don't surface explicit risks, reason from data patterns",
    "2-3 paragraphs of synthesis (minimum",
    "one markdown table of sources (publication",
    "do not repeat anything already said in the executive summary",
    "this is a desk-note, not a methods statement",
    "source breadth: list every distinct publication",
    "url column: the url column must contain the exact",
    "formatting density — mandatory",
    "accuracy mandate: every number, date, name",
    "no background-knowledge fill-in",
    "no false completeness",
    "numeric consistency: when the same metric",
    "table hygiene: every column must have real values",
    "table completeness: if a data row has no values",
    "no duplicate tables: each table must appear exactly once",
    "sector performance table: only include a time-series table",
    "never write raw json inside the \"report\" string",
    "never use unescaped double-quote characters",
    "no repetition across sections",
    "signed numbers for gains/losses",
    "narrated lists → tables",
    "narrated lists -> tables",
    "structure adapts to the topic",
    "these power the infographic stat-card strips",
    "required anchors (always present",
    "format ratio — structured content leads",
    "ai image rules",
    "never ask for text, numbers,",
    "length target: the \"report\" field should land",
)


def _strip_leaked_prompt_tail(report_text: str) -> str:
    """Truncate report_text at the first sign the model started echoing its
    own SYSTEM_PROMPT instructions instead of writing content."""
    if not report_text:
        return report_text
    lowered = report_text.lower()
    earliest = None
    for marker in _LEAKED_PROMPT_MARKERS:
        idx = lowered.find(marker)
        if idx != -1 and (earliest is None or idx < earliest):
            earliest = idx
    if earliest is None:
        return report_text
    cut = earliest
    heading_search = report_text[:earliest]
    # Only walk back to a heading if one starts shortly before the leak
    # (e.g. the leaked text's own orphaned "### Key Findings" restart) —
    # bounding the lookback avoids accidentally chopping off real, unrelated
    # earlier sections if a marker match ever turns up far from any heading.
    for m in reversed(list(re.finditer(r"^#{1,3}[ \t]*\S.*$", heading_search, re.MULTILINE))):
        if earliest - m.start() <= 400:
            cut = m.start()
            if cut > 0 and heading_search[cut - 1] == "\n":
                cut -= 1  # also drop the blank line right before the heading
            break
    truncated = report_text[:cut].rstrip()
    log.warning(
        "Report: detected leaked system-prompt instructions in report body — "
        "truncated from %d to %d chars", len(report_text), len(truncated),
    )
    return truncated


def _strip_citation_markers(text: str) -> str:
    if not text:
        return text
    text = _CITATION_MARKER_RE.sub("", text)
    # Same "--" → em dash normalization as the title (see _sanitize_title) —
    # applied at report-body level too since this function is the one choke
    # point every parse/salvage/repair path already runs the final report
    # text through.
    text = re.sub(r"(?<=\S)\s--\s(?=\S)", " — ", text)
    return text


_TITLE_PREAMBLE_RE = re.compile(
    r"^(?:so,?\s+(?:you'?re?|we'?re?|let'?s?)|you'?re?\s+looking|i'?ve?\s+(?:prepared|compiled|created)|"
    r"let'?s\s+(?:look|explore|dive|examine)|here'?s?\s+(?:a|an|the|your)|based\s+on|"
    r"according\s+to|as\s+per|in\s+light\s+of|"
    r"looking\s+at|looking\s+for|exploring|understanding|a\s+look\s+at|an?\s+(?:analysis|overview|in-depth|deep|guide)\s+of|"
    r"the\s+following|below\s+(?:is|are)|"
    # Catch "The X is/are/was/were/has/have/will/would/could/should/may/might ..."
    r"the\s+\w+(?:\s+\w+){0,4}\s+(?:is|are|was|were|has|have|will|would|could|should|may|might)\s)",
    re.IGNORECASE,
)

# Catches full-sentence titles that don't start with one of the preamble
# phrases above but still read as a sentence rather than a noun phrase —
# e.g. "The minimum investment amounts ... are as follows", or anything
# ending in a colon (which always introduces a sentence, not a heading),
# or ending with "..." / "…" (truncated LLM sentence used as title).
_TITLE_SENTENCE_RE = re.compile(
    r"(:\s*$|\bas\s+follows\s*$|\b(?:are|is|were|have\s+been|has\s+been)\s+"
    r"(?:as\s+follows|below|here|outlined|shown|listed|summarized|summarised)\b|"
    r"\.{3}\s*$|…\s*$|,\s+with\s+[a-z]|,\s+as\s+[a-z]|,\s+while\s+[a-z])",
    re.IGNORECASE,
)

def _sanitize_title(title: str, question: str) -> str:
    """Post-process LLM-generated title: strip conversational openers,
    fall back to a clean noun-phrase from the question."""
    if not title:
        title = question
    title = title.strip().rstrip(".")
    # Models frequently type "--" as a stand-in for an em dash (e.g. "Overview
    # -- July 2026"). Normalize to a real em dash so it doesn't read as a
    # typo on the cover page.
    title = re.sub(r"\s+--\s+", " — ", title)
    if _TITLE_PREAMBLE_RE.match(title) or _TITLE_SENTENCE_RE.search(title):
        # Extract noun-phrase from question
        q = re.sub(r"(?i)^(tell me about|what are|what is|show me|give me|find me|list the|compare|analyse|analyze|explain|explore|summarize|summarise)\s+", "", question.strip())
        q = q.rstrip("?!.").strip()
        # Capitalise words
        title = q[:80] if len(q) >= 5 else "Research Report"
    # Title-case if all lowercase
    if title == title.lower():
        title = title.title()
    if len(title) > 120:
        title = title[:120].rsplit(" ", 1)[0].rstrip(",;:-—") + "…"
    return title




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


_MD_TABLE_ROW_RE = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$")
_MD_TABLE_SEP_RE = re.compile(r"^[ \t]*\|[ \t:|\-]+\|[ \t]*$")
_MD_HEADING_RE   = re.compile(r"^#{1,6}\s+(.+?)\s*$")

# Section headings whose tables are structural/metadata, not analytical data
# (e.g. the "Data Sources & Methodology" table listing publications/URLs) —
# these should never be turned into a "chart" even though they technically
# satisfy the >=2 cols / >=2 rows size check below.
_NON_CHART_HEADING_RE = re.compile(
    r"\b(sources?|methodology|data\s+sources?|references?|citations?|bibliography|appendix)\b",
    re.IGNORECASE,
)


def _normalize_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.strip().lower())


def _chart_label_set(ch: dict) -> set[str]:
    """All row/category labels a chart already covers, normalized for
    loose comparison (case/punctuation/whitespace-insensitive)."""
    if ch.get("type") == "table":
        rows = ch.get("rows") or []
        return {_normalize_label(row[0]) for row in rows if row}
    labels: set[str] = set()
    for s in ch.get("series") or []:
        for pt in s.get("data") or []:
            lbl = pt.get("label")
            if lbl:
                labels.add(_normalize_label(str(lbl)))
    return labels


def _table_duplicates_existing_chart(header_cells: list[str], rows: list[list[str]], existing_charts: list) -> bool:
    """
    True if this markdown table's row labels are (near-)identical to a
    chart that's already in existing_charts — e.g. the model writes a
    "NIFTY 50 / SENSEX / NIFTY BANK / NIFTY IT" table in the report body
    AND already charted that exact same index data as a bar chart earlier
    in its JSON. Converting the table into a second Datawrapper "chart"
    in that case just repeats the same 4 numbers a second time as an
    inert table sitting next to the real chart — readers see one chart's
    worth of information padded out to look like two.
    """
    if not existing_charts or not rows:
        return False
    table_labels = {_normalize_label(row[0]) for row in rows if row}
    if not table_labels:
        return False
    for ch in existing_charts:
        chart_labels = _chart_label_set(ch)
        if not chart_labels:
            continue
        overlap = table_labels & chart_labels
        # Near-identical label sets (allowing for the table listing a
        # superset/subset, e.g. an extra footnote row) — same underlying
        # data being shown twice.
        smaller = min(len(table_labels), len(chart_labels))
        if smaller and len(overlap) / smaller >= 0.75:
            return True
    return False


def _extract_markdown_tables(report_text: str, existing_charts: list) -> tuple[str, list]:
    """Find markdown pipe-tables (header row + |---|---| separator + data rows)
    in the report body, turn each into a {"type": "table", ...} chart-spec so
    it gets published as a real Datawrapper table, and replace it inline with
    a [CHART_n] placeholder — same convention already used for bar/line/pie
    charts. Tables too small to bother with (a single data row, or a row
    that's really just a one-off list) are left as plain markdown. Tables
    under a Sources/Methodology/References-style heading are also left as
    plain markdown — they're report metadata, not chartable data.
    """
    lines = report_text.split("\n")
    charts = list(existing_charts)
    out: list[str] = []
    last_heading = ""
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        h = _MD_HEADING_RE.match(line.strip())
        if h:
            last_heading = h.group(1).strip()

        header_m = _MD_TABLE_ROW_RE.match(line)
        if header_m and i + 1 < n and _MD_TABLE_SEP_RE.match(lines[i + 1]):
            header_cells = [c.strip() for c in header_m.group(1).split("|")]
            j = i + 2
            rows: list[list[str]] = []
            while j < n:
                row_m = _MD_TABLE_ROW_RE.match(lines[j])
                if not row_m or _MD_TABLE_SEP_RE.match(lines[j]):
                    break
                cells = [c.strip() for c in row_m.group(1).split("|")]
                if len(cells) < len(header_cells):
                    cells += [""] * (len(header_cells) - len(cells))
                rows.append(cells[: len(header_cells)])
                j += 1

            is_metadata_table = bool(_NON_CHART_HEADING_RE.search(last_heading))
            is_duplicate_of_chart = _table_duplicates_existing_chart(header_cells, rows, charts)
            if len(header_cells) >= 2 and len(rows) >= 2 and not is_metadata_table and not is_duplicate_of_chart:
                # Reports never show raw URLs, in the Data Sources table or
                # anywhere else — drop any URL/Link column entirely (not
                # just blank bad cells in it) so a stray link column the
                # model adds despite the prompt's instructions still can't
                # make it into the rendered PDF. Unambiguous header names
                # ("URL", "Link"...) are dropped outright; an ambiguous name
                # like "Source" or "Website" (which could just as easily
                # hold a publication NAME, which we want to keep) is only
                # dropped if most of its actual cell values look like
                # http(s) links, not merely from its header text.
                unambiguous_url_headers = {"url", "link", "source url", "web link", "hyperlink"}
                ambiguous_url_headers = {"source", "website"}

                def _is_url_col(idx: int, header: str) -> bool:
                    h = header.strip().lower()
                    if h in unambiguous_url_headers:
                        return True
                    if h in ambiguous_url_headers:
                        vals = [row[idx].strip() for row in rows if idx < len(row) and row[idx].strip()]
                        if not vals:
                            return False
                        return sum(1 for v in vals if re.match(r"^https?://", v)) >= max(1, len(vals) * 0.6)
                    return False

                url_col_idxs = {idx for idx, h in enumerate(header_cells) if _is_url_col(idx, h)}
                if url_col_idxs and len(header_cells) - len(url_col_idxs) >= 2:
                    header_cells = [h for idx, h in enumerate(header_cells) if idx not in url_col_idxs]
                    rows = [[c for idx, c in enumerate(row) if idx not in url_col_idxs] for row in rows]

                charts.append({
                    "type":    "table",
                    "title":   last_heading or "Data Table",
                    "columns": header_cells,
                    "rows":    rows,
                })
                out.append(f"[CHART_{len(charts)}]")
                i = j
                continue
            # Too small, a Sources/Methodology-style metadata table, or a
            # near-duplicate of data already shown in an existing chart —
            # leave it as plain markdown rather than charting it.
            if is_duplicate_of_chart:
                log.info("Skipped charting markdown table %r — duplicates an existing chart's data",
                          (last_heading or "Data Table")[:60])

        out.append(line)
        i += 1

    return "\n".join(out), charts


# Groq context limit: ~6K tokens input. Skip Groq (go straight to Gemini)
# if the combined payload is over this. This is the TOTAL budget (system +
# user), not just the user prompt — SYSTEM_PROMPT alone runs ~16K chars, so
# checking only the user prompt against 40K let combined payloads reach
# ~56K chars and 413 unpredictably depending on content density.
GROQ_MAX_PROMPT_CHARS = 40_000  # total system+user budget Groq will reliably accept


async def call_groq(user_prompt: str) -> str:
    keys = get_groq_keys()
    if not keys:
        log.warning("Groq: no keys configured for report generation")
        return ""

    # Pre-flight size check — measure the REAL combined payload (system + user,
    # untrimmed) against the budget. If it's already over, don't even attempt
    # Groq: trimming a 57K-char prompt down to fit guts most of the source
    # content anyway, and the previous "try anyway, 413, retry with harder
    # trim" dance just burned every key with 2 doomed requests each (8 wasted
    # round-trips, ~2s, before ever reaching Gemini). Skip straight there.
    combined = len(SYSTEM_PROMPT) + len(user_prompt)
    if combined > GROQ_MAX_PROMPT_CHARS:
        log.info(
            "Groq: skipping — combined payload %d chars (system %d + user %d) "
            "exceeds %d char budget; going straight to Gemini",
            combined, len(SYSTEM_PROMPT), len(user_prompt), GROQ_MAX_PROMPT_CHARS,
        )
        return ""

    log.info("Groq: calling for report generation with %d key(s)  prompt=%d chars", len(keys), len(user_prompt))

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
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 32768,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
            if res.status_code == 413:
                # We already checked the size upfront, so a 413 here means our
                # estimate was wrong (denser tokenization, Groq-side quirk,
                # etc.) — not something the next key will fix. Bail out to
                # Gemini immediately instead of repeating the same oversized
                # request against every remaining key.
                log.warning(
                    "Groq 413 on key ...%s despite passing the %d-char pre-check "
                    "— aborting Groq for this request, going straight to Gemini",
                    key[-4:], GROQ_MAX_PROMPT_CHARS,
                )
                mark_rate_limited(key, 120_000)
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
                if len(text) < MIN_REPORT_CHARS:
                    # Length is no longer a retry trigger — a short report reflects
                    # thin source data, not a bad generation (see report.py notes
                    # on call_gemini for the full rationale). Retrying here just
                    # burns API keys for the same shallow sourcing. Accept as-is
                    # and log the shortfall for visibility only.
                    log.info(
                        "Groq: report is %d chars (< %d target) — accepting as-is, no retry",
                        len(text), MIN_REPORT_CHARS,
                    )
                return text
        except Exception as exc:
            log.warning("Groq exception on key ...%s: %s", key[-4:], exc)
            continue

    log.error("Groq: all keys exhausted for report generation")
    return ""


# ---------------------------------------------------------------------------
# Gemini model table — ordered BEST FIRST (quality + output token limit).
# We try the highest-capability model first and fall back only on 503/429/404.
# Output token limits matter most for long reports (target 18K+ chars ≈ 9K tokens).
#
#  Model                  | Max output tokens | Notes
#  -----------------------|-------------------|-------------------------------
#  gemini-2.5-flash       |        65 536     | Fast + high quality
#  gemini-3.5-flash       |        65 536     | Next-gen flash
#  gemini-3-flash-preview |        65 536     | Preview of Gemini 3
#  gemini-2.5-flash-lite  |        32 768     | Smallest output window — last resort
#
# Removed: gemini-2.0-flash / gemini-2.0-flash-lite (retired June 2026)
# Removed: gemini-2.5-pro — confirmed 0/0 RPM/TPM/RPD quota on every free-tier
# project we hold (see AI Studio Rate Limit dashboards, checked 2026-07-03).
# It can never succeed until at least one project has billing enabled, so
# trying it first just burns ~1-2s and a key-level rate-limit mark on every
# single report/extraction call for no chance of success. Re-add it once
# billing is enabled on a project, ideally as a first-choice model rather
# than folded back into this fallback order.
# ---------------------------------------------------------------------------
# ── Gemini key format filter ────────────────────────────────────────────────
# Google has been migrating Gemini API keys from the legacy "AIzaSy..."
# Standard-key format to a new "AQ.Ab..." Auth-key format since June 2026.
# New keys issued in AI Studio now come out as AQ. by default; unrestricted
# AIza Standard keys already stopped working as of June 19, 2026, and ALL
# AIza keys — restricted or not — are cut off entirely from September 2026.
# Both formats authenticate the same way against this same REST endpoint
# (generativelanguage.googleapis.com) with the same ?key= query param, so
# both are accepted here. What must still be excluded is gen-lang-client-*
# keys, which are Vertex AI / Google Cloud service-account-style keys that
# need OAuth and return 400 Bad Request against this REST endpoint.
def _is_rest_api_key(key: str) -> bool:
    return key.startswith("AIzaSy") or key.startswith("AQ.")


GEMINI_MODELS = [
    "gemini-2.5-flash",        # 65 536 output tokens — fast + high quality
    "gemini-3.5-flash",        # 65 536 output tokens — next-gen flash
    "gemini-3-flash-preview",  # 65 536 output tokens — Gemini 3 preview
    "gemini-2.5-flash-lite",   # 32 768 output tokens — last resort (smallest window)
]

# Minimum acceptable report length — see the "target 3500-4500 words" mandate
# in SYSTEM_PROMPT. Kept as one constant so Groq/Gemini paths (and any future
# provider) enforce the same floor.
MIN_REPORT_CHARS = 22_000

# Hard wall-clock budget for the ENTIRE call_gemini() attempt loop, across all
# keys and models combined. Without this, a Gemini outage (mass 503s) or a
# string of "too short" rejections walks every key×model combination
# sequentially — with 22 keys × 4 models × up to ~30s per attempt, that's
# tens of minutes, which starves the request well past any reasonable client
# or proxy timeout and leaves the user watching a spinner indefinitely.
# Past this budget we stop trying and fall back to the best candidate seen.
GEMINI_TIME_BUDGET_SECONDS = 75
# Cap on keys tried per model before moving on — with many keys configured,
# exhausting all of them against one struggling/overloaded model is rarely
# worth it; better to give the remaining models their turn sooner.
GEMINI_MAX_KEYS_PER_MODEL = 6

# Per-model max output tokens — used to set the right ceiling per attempt.
# Setting this too high on flash-lite causes it to hang; match the actual model limit.
_GEMINI_MAX_OUTPUT = {
    "gemini-2.5-flash":        65_536,
    "gemini-3.5-flash":        65_536,
    "gemini-3-flash-preview":  65_536,
    "gemini-2.5-flash-lite":   32_768,
}


async def call_gemini(user_prompt: str) -> tuple[str, str]:
    keys = get_gemini_keys()
    if not keys:
        log.warning("Gemini: no keys configured for report generation")
        return "", ""

    # Filter to keys usable against this REST endpoint — see _is_rest_api_key
    # for why both AIzaSy (legacy) and AQ. (current) formats are accepted,
    # while gen-lang-client-* (Vertex AI) keys are excluded.
    rest_keys = [k for k in keys if _is_rest_api_key(k)]
    if not rest_keys:
        log.warning("Gemini: no AIzaSy*/AQ.* keys available — all keys are gen-lang-client type")
        return "", ""

    log.info("Gemini: attempting report generation with %d key(s), %d models", len(rest_keys), len(GEMINI_MODELS))

    # Build attempt queue: model varies slowest (best model tried across ALL keys
    # before falling back to next model). Within each model, prefer non-rate-limited
    # keys first, then rate-limited keys as a last resort.
    def _key_order(model: str) -> list:
        ok  = [k for k in round_robin(rest_keys) if not is_rate_limited(k) and not is_rate_limited(f"{k}:{model}")]
        rl  = [k for k in rest_keys if is_rate_limited(k) or is_rate_limited(f"{k}:{model}")]
        return ok + rl  # non-RL keys first

    attempts = []
    for model in GEMINI_MODELS:
        # Cap keys tried per model — see GEMINI_MAX_KEYS_PER_MODEL above.
        for key in _key_order(model)[:GEMINI_MAX_KEYS_PER_MODEL]:
            attempts.append((key, model))

    # Models confirmed unavailable (e.g. 404) during this call — once a model
    # is dead, skip its remaining key attempts without aborting the rest of
    # the queue (other models must still get their turn).
    dead_models: set[str] = set()

    loop_start = time.perf_counter()

    for key, model in attempts:
        if time.perf_counter() - loop_start > GEMINI_TIME_BUDGET_SECONDS:
            log.warning(
                "Gemini: time budget (%ds) exhausted — stopping attempt loop early",
                GEMINI_TIME_BUDGET_SECONDS,
            )
            break
        if model in dead_models:
            continue
        if is_rate_limited(f"{key}:{model}"):
            continue
        try:
            log.debug("Gemini: trying model=%s key=...%s", model, key[-4:])
            t0 = time.perf_counter()
            max_out = _GEMINI_MAX_OUTPUT.get(model, 65_536)
            generation_config = {
                "maxOutputTokens": max_out,
                "temperature": 0.1,
                # NOTE: responseMimeType:"application/json" is intentionally NOT set.
                # When set, Gemini hard-truncates output mid-JSON at the token limit,
                # causing JSON parse failures on long reports. Without it, Gemini
                # finishes the object cleanly; the fence-strip pass below handles any
                # stray ``` Gemini might add around the output.
            }
            # Suppress thinking tokens — keep full token budget for report JSON.
            # Gemini 2.5 family uses thinkingBudget:0; Gemini 3.x uses thinkingLevel.
            # Mixing these across families causes a 400 error.
            _GEMINI_25 = {"gemini-2.5-flash", "gemini-2.5-flash-lite"}
            _GEMINI_3X = {"gemini-3-flash-preview", "gemini-3.5-flash"}
            if model in _GEMINI_25:
                generation_config["thinkingConfig"] = {"thinkingBudget": 0}
            elif model in _GEMINI_3X:
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
                    # Model doesn't exist — no point trying other keys for this model,
                    # but the remaining models still deserve their attempts.
                    log.warning("Gemini 404 — model=%s is deprecated/unavailable, skipping all keys for it", model)
                    dead_models.add(model)
                continue
            text = res.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if text:
                elapsed = (time.perf_counter() - t0) * 1000
                log.info("Gemini: report generated in %.0fms (%d chars) model=%s key=...%s",
                         elapsed, len(text), model, key[-4:])
                # Reject under-length reports — the system prompt mandates ≥15K chars.
                # Treat short output as a soft failure and try the next model/key.
                # Check finish_reason — MAX_TOKENS means Gemini was cut off mid-output
                finish_reason = (
                    res.json()
                    .get("candidates", [{}])[0]
                    .get("finishReason", "")
                )
                if finish_reason == "MAX_TOKENS":
                    log.warning(
                        "Gemini: MAX_TOKENS — output was truncated mid-response model=%s key=...%s (%d chars) — retrying next slot",
                        model, key[-4:], len(text),
                    )
                    continue
                if len(text) < MIN_REPORT_CHARS:
                    # Length is no longer a retry trigger (see note by
                    # MIN_REPORT_CHARS) — short output usually means thin
                    # source data, not a bad generation. Retrying here just
                    # burns keys re-asking the same shallow sources the same
                    # question. Log for visibility and fall through to the
                    # structural checks below (runaway/legacy-heading/exec
                    # summary) — those still guard against genuinely broken
                    # output; only the character-count floor is relaxed.
                    log.info(
                        "Gemini: report is %d chars (< %d target) — accepting as-is, no retry",
                        len(text), MIN_REPORT_CHARS,
                    )
                # Reject runaway-length output. Target is ~3500-4500 words
                # (roughly 22K-38K chars incl. markdown/tables). Output has
                # been observed to balloon to 400K+ chars when the model,
                # chasing the length mandate, exhausts real content and
                # starts repeating/echoing text (including its own
                # instructions) to keep the character count climbing. Such
                # output is never usable even where _strip_leaked_prompt_tail
                # can clean it up downstream, so treat it as a soft failure
                # here and retry a fresh key/model instead of paying for a
                # ~450K-char generation on every attempt.
                if len(text) > 60_000:
                    log.warning(
                        "Gemini: report suspiciously long (%d chars > 60000) — likely runaway/"
                        "repeating generation — model=%s key=...%s — retrying next slot",
                        len(text), model, key[-4:],
                    )
                    continue
                # Reject stale-structure output: the prompt no longer has a "Methodology"
                # section (replaced by a data-point "Executive Summary"), so a report that
                # still carries the old heading means the model drifted back to a fixed,
                # memorized shape rather than following the current instructions. Recycle
                # to the next key/model slot rather than shipping it.
                _lower_text = text.lower()
                if re.search(r"##\s*2\.\s*methodology\b", _lower_text):
                    log.warning(
                        "Gemini: output reverted to legacy 'Methodology' heading — "
                        "model=%s key=...%s — retrying next slot for fresh structure",
                        model, key[-4:],
                    )
                    continue
                if "executive summary" not in _lower_text:
                    log.warning(
                        "Gemini: output missing required Executive Summary section — "
                        "model=%s key=...%s — retrying next slot",
                        model, key[-4:],
                    )
                    continue
                return text, model
            log.warning("Gemini: empty response from model=%s key=...%s", model, key[-4:])
        except Exception as exc:
            log.warning("Gemini exception model=%s key=...%s: %s", model, key[-4:], exc)
            continue

    log.error("Gemini: all key×model combinations exhausted")
    return "", ""

# ── AI image generation (Imagen + Gemini) ───────────────────────────────────
# Used only for the rare, optional editorial/illustrative images described in
# AI IMAGE RULES above — never for chart/data visuals, which are handled
# entirely by the charts pipeline. Reuses the same key rotation as
# call_gemini() so image requests share the existing rate-limit bookkeeping.
#
# Imagen models are tried FIRST, not the Gemini "Nano Banana" flash-image
# models — this project's free-tier key has real quota for Imagen (25
# requests/day each on imagen-4.0-*) but 0/0/0 quota for every "Nano Banana"
# / Gemini flash-image model, per the account's own rate-limit dashboard.
# Every attempt against a Gemini image model on this tier was therefore
# guaranteed to fail before it even ran (quota already exhausted at 0),
# which is a very different failure mode than "no image data in response"
# — it's "this tier can't call this model at all". Gemini's flash-image
# models are kept as a fallback in case the account's quota changes later
# (e.g. a paid tier), but Imagen is what's actually usable today.
IMAGEN_MODELS = [
    "imagen-4.0-fast-generate-001",   # highest RPM quota (150/min) — tried first
    "imagen-4.0-generate-001",        # standard quality, 75/min
    "imagen-4.0-ultra-generate-001",  # highest quality, lowest quota — last resort
]
GEMINI_IMAGE_MODELS = [
    "gemini-2.5-flash-image",       # GA image-generation model
    "gemini-3.1-flash-image-preview",  # newer preview, tried if 2.5 is unavailable
]


async def _call_pollinations_image(client: httpx.AsyncClient, prompt: str) -> bytes | None:
    """Pollinations AI (image.pollinations.ai) — free, unlimited, no API key.
    Simple GET against a REST endpoint with the prompt URL-encoded into the
    path; returns raw image bytes directly (no JSON envelope to unwrap).
    Tried FIRST, ahead of Imagen/Gemini flash-image, since this account's
    Gemini keys currently have zero image-generation quota on every model
    (see generate_gemini_image below) while Pollinations has none of that
    quota friction."""
    import urllib.parse as _urlparse
    import random as _random
    encoded = _urlparse.quote(prompt, safe="")
    url = f"https://image.pollinations.ai/prompt/{encoded}"
    params = {
        "width": "1280", "height": "960", "nologo": "true", "model": "flux",
        # enhance=true routes the prompt through Pollinations' own LLM prompt
        # rewriter before generation, which fleshes out composition/lighting/
        # detail the way a human-tuned prompt would — noticeably reduces the
        # "generic AI stock photo" look versus sending the raw prompt as-is.
        "enhance": "true",
        "seed": str(_random.randint(1, 2_000_000_000)),  # avoid returning a cached image for a repeated prompt
    }
    try:
        res = await client.get(url, params=params, timeout=60)
        if res.status_code == 200 and res.content and len(res.content) > 500:
            return res.content
        log.warning("Image gen: Pollinations HTTP %d (%d bytes)", res.status_code, len(res.content or b""))
    except Exception as exc:
        log.warning("Image gen: Pollinations exception: %s", exc)
    return None


async def _call_imagen(client: httpx.AsyncClient, model: str, key: str, prompt: str) -> httpx.Response:
    """Imagen models use the Vertex/GenAI `:predict` endpoint — a completely
    different request/response shape than the Gemini chat models'
    `:generateContent` (instances/predictions instead of contents/
    candidates). There's no generationConfig/responseModalities here;
    Imagen is images-only by definition."""
    return await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:predict?key={key}",
        json={"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}},
    )


async def _call_gemini_flash_image(client: httpx.AsyncClient, model: str, key: str, prompt: str) -> httpx.Response:
    return await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        # NOTE: generationConfig.responseModalities used to be omitted
        # entirely. gemini-2.5-flash-image (a dedicated, image-only model)
        # tolerates that and defaults to returning an image anyway — but
        # gemini-3.1-flash-image-preview is an interleaved text+image
        # model, and without an explicit responseModalities it can default
        # to TEXT-only, silently producing zero image parts. Setting both
        # modalities explicitly, per Google's own docs, makes image output
        # the guaranteed behavior for both models instead of an
        # implementation-defined default.
        json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        },
    )


async def generate_gemini_image(prompt: str) -> bytes | None:
    """Generate a single PNG/JPEG image from a text prompt. Tries Pollinations
    AI first (free, unlimited, no API key — see _call_pollinations_image),
    then falls back to Imagen and Gemini's flash-image models in case
    Pollinations is briefly down. Returns raw image bytes, or None on
    failure — callers must treat a miss as "skip this image", never as a
    hard error."""
    async with httpx.AsyncClient(timeout=60) as client:
        img = await _call_pollinations_image(client, prompt)
    if img:
        log.info("Image gen: generated via Pollinations (%d bytes)", len(img))
        return img
    log.warning("Image gen: Pollinations failed, falling back to Imagen/Gemini")

    keys = get_gemini_keys()
    rest_keys = [k for k in keys if _is_rest_api_key(k)]
    if not rest_keys:
        log.warning("Image gen: no usable API keys configured")
        return None

    all_models = [(m, "imagen") for m in IMAGEN_MODELS] + [(m, "gemini") for m in GEMINI_IMAGE_MODELS]
    attempts = [
        (key, model, family)
        for model, family in all_models
        for key in round_robin(rest_keys)
        if not is_rate_limited(key) and not is_rate_limited(f"{key}:{model}")
    ]
    # Models confirmed unavailable (404) — skip their remaining key attempts
    # without aborting attempts for the other model(s) still in the queue.
    dead_models: set[str] = set()
    for key, model, family in attempts:
        if model in dead_models:
            continue
        try:
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=60) as client:
                res = (
                    await _call_imagen(client, model, key, prompt)
                    if family == "imagen"
                    else await _call_gemini_flash_image(client, model, key, prompt)
                )
            if res.status_code == 429:
                mark_rate_limited(key, 60_000)
                mark_rate_limited(f"{key}:{model}", 60_000)
                continue
            if res.status_code == 403:
                mark_rate_limited(key, 24 * 60 * 60_000)
                continue
            if not res.is_success:
                log.warning("Image gen: HTTP %d model=%s key=...%s", res.status_code, model, key[-4:])
                if res.status_code == 404:
                    dead_models.add(model)  # this model isn't available at all — skip straight to the next model
                continue

            body = res.json()
            if family == "imagen":
                predictions = body.get("predictions") or []
                if not predictions:
                    # Imagen returns 200 with an empty/absent `predictions`
                    # list when every candidate image was filtered out by
                    # the safety filter — there's no separate
                    # `promptFeedback` block like the chat models have.
                    log.warning("Image gen: Imagen returned no predictions model=%s key=...%s", model, key[-4:])
                    continue
                b64 = predictions[0].get("bytesBase64Encoded")
                if b64:
                    elapsed = (time.perf_counter() - t0) * 1000
                    log.info("Image gen: generated in %.0fms model=%s key=...%s", elapsed, model, key[-4:])
                    import base64 as _b64mod
                    return _b64mod.b64decode(b64)
                log.warning("Image gen: prediction had no image bytes model=%s key=...%s", model, key[-4:])
            else:
                candidates = body.get("candidates") or []
                if not candidates:
                    # A 200 response with zero candidates means the prompt was
                    # blocked (safety filter / recitation / etc.) — the reason
                    # lives in promptFeedback.blockReason. The old code did
                    # `res.json().get("candidates", [{}])[0]`: since "candidates"
                    # WAS present in the body (just empty), that default never
                    # kicked in and `[0]` raised an IndexError on an empty list —
                    # caught by the blanket `except Exception` below and logged
                    # as an opaque "Gemini image exception", hiding the real
                    # cause. Handle it explicitly here instead.
                    block_reason = (body.get("promptFeedback") or {}).get("blockReason")
                    log.warning("Image gen: no candidates returned model=%s key=...%s blockReason=%s",
                                model, key[-4:], block_reason or "unknown")
                    continue
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        elapsed = (time.perf_counter() - t0) * 1000
                        log.info("Image gen: generated in %.0fms model=%s key=...%s", elapsed, model, key[-4:])
                        import base64 as _b64mod
                        return _b64mod.b64decode(inline["data"])
                log.warning("Image gen: no image data in response model=%s key=...%s", model, key[-4:])
        except Exception as exc:
            log.warning("Image gen exception model=%s key=...%s: %s", model, key[-4:], exc)
            continue
    log.warning("Image gen: all key×model combinations exhausted for prompt=%r", prompt[:80])
    return None


async def _generate_ai_report_images(raw_images: list, max_images: int = 2) -> tuple[list[dict], list[bool]]:
    """Turn the model's requested image PROMPTS (see AI IMAGE RULES) into real
    generated images. Returns ({"url": "data:image/...;base64,...", "caption"})
    entries plus a keep-mask aligned to raw_images' original order, in the same
    shape _validate_image_selections/_remap_web_image_placeholders expect —
    a failed generation is simply dropped (mask False), never a hard error."""
    # Appended to every image prompt so report images share one cohesive,
    # premium editorial look tied to the brand (deep navy + warm gold, the
    # same palette used across the PDF's header/cover/accent elements)
    # instead of each image landing on whatever generic style the model
    # defaults to on its own. Composition/quality directives here are
    # deliberately generic (never topic-specific) since the report's own
    # prompt already carries the specific scene — this only shapes *how*
    # that scene is rendered, not *what* it depicts.
    _BRAND_STYLE_SUFFIX = (
        ", premium editorial photography, cinematic natural lighting, shallow depth of field, "
        "rich detail and texture, sophisticated muted color grade with deep navy blue and warm "
        "gold accent tones, shot on a full-frame camera, magazine feature quality, no text, "
        "no watermark, no logos"
    )

    candidates: list[tuple[int, str, str]] = []  # (original_index, prompt, caption)
    for i, entry in enumerate(raw_images or []):
        if not isinstance(entry, dict):
            continue
        prompt = str(entry.get("prompt") or "").strip()
        if not prompt:
            continue
        caption = str(entry.get("caption") or "").strip()[:160]
        candidates.append((i, f"{prompt}{_BRAND_STYLE_SUFFIX}", caption))
        if len(candidates) >= max_images:
            break

    mask = [False] * len(raw_images or [])
    if not candidates:
        return [], mask

    results = await asyncio.gather(
        *[generate_gemini_image(prompt) for _, prompt, _ in candidates],
        return_exceptions=True,
    )

    import base64 as _b64
    final: list[dict] = []
    for (orig_idx, prompt, caption), img_bytes in zip(candidates, results):
        if isinstance(img_bytes, Exception) or not img_bytes:
            continue
        b64 = _b64.b64encode(img_bytes).decode("ascii")
        final.append({"url": f"data:image/png;base64,{b64}", "caption": caption})
        mask[orig_idx] = True

    if final:
        log.info("Report: generated %d/%d AI image(s) via Gemini", len(final), len(candidates))
    return final, mask


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
    # Both AIzaSy* (legacy) and AQ.* (current) key formats work with the REST API used here.
    vision_keys = [k for k in keys if _is_rest_api_key(k)]
    if not vision_keys:
        log.warning("extract_data_from_images: no AIzaSy*/AQ.* Gemini keys available for vision")
        return ""

    available_keys = [k for k in vision_keys if not is_rate_limited(k)]
    if not available_keys:
        available_keys = vision_keys

    # gemini-2.5-pro deliberately excluded — 0/0 RPM/TPM/RPD quota on every
    # free-tier project we hold (see report generation model list above for
    # the same rationale). It can only ever fail here, so trying it just
    # wastes a request slot and a timeout window.
    for key in available_keys[:3]:  # try up to 3 keys
        for model in ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash"]:
            try:
                # Shortened from 60s: a genuinely hung/degraded key should fail
                # fast so we can move on to the next key/model, not eat a full
                # minute per attempt (this previously caused ~3min stalls when
                # a key was silently hanging instead of returning a fast 429).
                async with httpx.AsyncClient(timeout=20) as client:
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
                if res.status_code == 403:
                    # Key is invalid/revoked/out of quota entirely (not just
                    # rate-limited) — this previously fell through to the
                    # generic "continue" below, which just tried the SAME
                    # dead key against the next model, guaranteeing 3 more
                    # wasted 403s before moving on, and never actually
                    # recorded the failure — so the very next call started
                    # fresh with this same dead key again. Ban it for the
                    # rest of this process's lifetime (matches the 24h ban
                    # already used elsewhere for 403s) and move to the next
                    # key immediately instead of burning through all models.
                    mark_rate_limited(key, 24 * 60 * 60_000)
                    log.warning("extract_data_from_images: key=...%s got 403 — banned for 24h", key[-4:])
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
                # Timeout or connection error — this key is degraded right now.
                # Previously this fell through silently and got retried against
                # every remaining model for the same key, each eating another
                # full timeout window. Mark it rate-limited (short backoff) and
                # move to the next key so a bad key can't stall the whole call.
                log.warning("extract_data_from_images error model=%s key=...%s: %s", model, key[-4:], exc)
                mark_rate_limited(key, 30_000)
                break

    log.warning("extract_data_from_images: all attempts failed")
    return ""


def _build_followup_search_query(question: str, conversation_context: str) -> str:
    """
    Follow-up report questions (e.g. "what about its peers", "and the risks")
    are often meaningless to a search engine on their own — they only make
    sense alongside the prior turn. Fold in a short topical hint from the
    most recent assistant response so the search engine gets grounding,
    without injecting a paragraph of prior answer text into the query — a
    search engine needs a topic phrase, not 300 characters of prose.
    """
    if not conversation_context.strip():
        return question

    # conversation_context is "User: ...\n\nAssistant: ...\n\nUser: ...\n\nAssistant: ..."
    # Grab the last Assistant block (and the User turn that produced it) —
    # that's the exchange this follow-up is actually building on.
    turns = [t.strip() for t in conversation_context.split("\n\n") if t.strip()]
    last_assistant = ""
    last_user = ""
    for idx in range(len(turns) - 1, -1, -1):
        if turns[idx].startswith("Assistant:"):
            last_assistant = turns[idx][len("Assistant:"):].strip()
            if idx > 0 and turns[idx - 1].startswith("User:"):
                last_user = turns[idx - 1][len("User:"):].strip()
            break
    if not last_assistant:
        return question

    # If that exchange was just smalltalk (e.g. "hi" → "Hello, nice to meet
    # you"), it carries no real topical grounding — folding it in actively
    # hurts search relevance rather than helping it, so treat this as if
    # there were no prior context at all.
    from routes.chat import is_smalltalk
    if is_smalltalk(last_user):
        return question

    # Minimal topic only — first clause of the prior answer, capped short —
    # enough to ground a pronoun ("its peers") without injecting prose.
    topic = re.split(r"[.\n]", last_assistant, maxsplit=1)[0].strip()[:60]
    return f"{question} {topic}".strip() if topic else question


_HISTORICAL_INTENT_RE = re.compile(
    r"\b(quarter|qtr|q[1-4]\b|quarterly|past \d+ (?:quarters?|months?|years?)|"
    r"since (?:19|20)\d{2}|yoy|qoq|year[- ]on[- ]year|year[- ]over[- ]year|"
    r"quarter[- ]on[- ]quarter|quarter[- ]over[- ]quarter|trend|historical|history|"
    r"over time|comparison|compare[ds]?|"
    r"last \d+ (?:quarters?|months?|years?))\b",
    re.IGNORECASE,
)


def _augment_query_for_historical_data(query: str) -> str:
    """When the question implies a time-series/quarter-over-quarter ask
    (e.g. "sector rotation past 8 quarters"), a bare Tavily search tends to
    surface today's-snapshot pages (day movers, current closing levels)
    rather than actual period-over-period data — the source site usually has
    both, but the plain query doesn't steer toward the right one. Append an
    explicit hint so the search leans toward quarter-comparison / historical
    tables instead of only the daily view.
    """
    if _HISTORICAL_INTENT_RE.search(query):
        return f"{query} quarter-wise comparison historical data table"
    return query


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
    # These are DIFFERENT from file_images above: file_images is full-page
    # renders of the uploaded PDF, used only for Gemini Vision text extraction
    # (extract_data_from_images below, which needs to read whole pages).
    # embedded_file_images is actual embedded raster images/charts/figures
    # pulled out of the PDF on the frontend (via pdf.js operator list) — this
    # is what may get embedded inline in the generated report. We never embed
    # a full page screenshot in the report itself, only real figures/charts
    # extracted from the file, and only if the file actually contains any.
    embedded_file_images: list[dict] = body.get("embeddedFileImages", [])
    session_id: str     = body.get("sessionId", "").strip()
    has_rag: bool       = bool(body.get("hasRag", False))
    # Separate from fileContext (which mixes in file text too) so the search
    # query builder below can target prior Q&A specifically — see
    # _build_followup_search_query.
    conversation_context: str = body.get("conversationContext", "")

    # Detect an explicit ask for MORE data points / charts / graphs / infographics
    # — either in the question itself ("give me more data points and graphs on
    # this") or in the prior turn the report follows on from. Previously this
    # was invisible to the report prompt: the generic CHART RULES section says
    # charts are "mandatory where data exists" but never overrides the model's
    # own judgment call on volume, so an explicit user request to go heavier on
    # data/visuals had no stronger instruction to actually act on — the model
    # kept producing its usual 2-3 charts regardless of what was asked. Now,
    # when this fires, an extra directive below raises the floor explicitly.
    _WANTS_MORE_DATA_RE = re.compile(
        r"\b(more|richer|deeper|additional|extra)\s+(data\s*points?|charts?|graphs?|"
        r"visuals?|infographics?|numbers|metrics|statistics)\b"
        r"|\binfographics?\b"
        r"|\bmore\s+granular\b",
        re.IGNORECASE,
    )
    wants_more_data_viz = bool(_WANTS_MORE_DATA_RE.search(question)) or bool(
        _WANTS_MORE_DATA_RE.search(conversation_context[-1500:])
    )

    # Defensive filter: Tavily (and any other internal search/aggregator domains)
    # must never be cited as if they were a publisher. Strip them here so they
    # can't end up numbered in the report body or the References section,
    # regardless of whether sources came from the client or our own search.
    _NON_CITABLE_DOMAINS = ("tavily.com",)
    sources = [
        s for s in sources
        if not any(d in (s.get("url") or "").lower() for d in _NON_CITABLE_DOMAINS)
    ]

    log.info("Report request: question=%r  sources=%d  fileImages=%d  fileContext=%d chars  rag=%s",
             question[:80], len(sources), len(file_images), len(file_context), has_rag)

    # Images Tavily turns up across whichever search call(s) below actually
    # run — shared across the file-first/self-search branches and the later
    # retry attempt, since they're all candidates for the SAME report and
    # there's no harm in a slightly larger pool. Filtered + capped just before
    # being put in front of the LLM.
    image_candidates_raw: list = []

    # ── RAG-grounded report: if files were indexed, use RAG full-coverage retrieval ──
    if has_rag and session_id:
        log.info("Report: RAG mode — full-coverage retrieval for session %s", session_id[:8])
        rag_result = await _rag_report(
            session_id=session_id,
            report_spec=question,
            report_type="comprehensive",
        )
        if rag_result.get("has_content") and rag_result.get("system_prompt"):
            returned_sources = rag_result.get("source_files", []) or []
            # Guard against session_id being a long-lived, cross-conversation ID
            # (same issue _fetch_okf_context guards against below): the RAG
            # service's /report endpoint retrieves full-coverage context scoped
            # ONLY by session_id, not by which file(s) were actually attached
            # THIS turn. A session that ever indexed an unrelated document days
            # or conversations ago (an equity research note, a fund factsheet)
            # will silently have it pulled into today's report too — showing up
            # as a fabricated extra "Data Source" the user never attached this
            # time. If this turn has an actual attachment, require at least one
            # returned source to match it (fuzzy substring, same as OKF below);
            # otherwise discard the RAG context entirely and fall through to
            # the direct file_context/OKF path built from what was really sent.
            current_filenames = set(re.findall(r"\[File:\s*([^\]]+?)\]", file_context))
            stale = False
            if current_filenames:
                norm_current  = {_normalize_filename(n) for n in current_filenames}
                norm_returned = {_normalize_filename(s) for s in returned_sources}
                stale = not any(nc in nr or nr in nc for nc in norm_current for nr in norm_returned)
            if stale:
                log.warning(
                    "Report: RAG source_files %s don't match this turn's attachment(s) %s "
                    "— discarding stale cross-session RAG context",
                    returned_sources, current_filenames,
                )
            else:
                # Build a rich user prompt that includes the RAG grounded system prompt
                rag_file_context = rag_result["system_prompt"]
                log.info("Report: RAG retrieved %d chunks from %s",
                         rag_result.get("retrieved", 0), returned_sources)
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
            search_query = _augment_query_for_historical_data(
                _build_followup_search_query(question, conversation_context)
            )
            searched = await _tavily_search(
                search_query, max_results=20, min_results=10,
                historical_intent=bool(_HISTORICAL_INTENT_RE.search(question)),
            )
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
        search_query = _augment_query_for_historical_data(
            _build_followup_search_query(question, conversation_context)
        )
        if search_query != question:
            log.info("Report: search query enriched (%d → %d chars): %r",
                      len(question), len(search_query), search_query[:150])
        searched = await _tavily_search(
            search_query, max_results=20,
            historical_intent=bool(_HISTORICAL_INTENT_RE.search(question)),
        )
        sources = [
            {"title": r["title"], "url": r["url"],
             "snippet": r["snippet"], "fullContent": r.get("fullContent", "")}
            for r in searched
        ]
        log.info("Report: self-search returned %d sources", len(sources))
    else:
        from routes.chat import _looks_like_ai_overview

    # A Tavily search coming back empty does NOT mean the topic is
    # unanswerable — it just means there's no news article/webpage that
    # matches it. Personal/advisory questions ("give me a business idea
    # given my income of X") are a common case: real, answerable, but not
    # something a web search will ever surface sources for. Previously this
    # bailed out entirely with a generic "Could not retrieve data" error,
    # even though the chat endpoint answers the exact same question fine
    # from the model's own knowledge. So: only hard-fail when the question
    # actually needs current/sourced data (prices, news, recent events) and
    # genuinely got nothing back. Otherwise fall through and let the model
    # write the report from its own reasoning/knowledge, with an explicit
    # instruction not to fabricate sources or citations.
    no_web_sources = not sources and not has_file_data
    if no_web_sources:
        needs_current_data = bool(re.search(
            r"\b(today|latest|current|this\s+(week|month|quarter|year)|"
            r"recent|news|price|prices|quote|nifty|sensex|stock|share\s+price|"
            r"market\s+(today|now)|breaking)\b",
            question, re.IGNORECASE,
        ))
        if needs_current_data:
            log.warning("Report: still no sources after self-search, and question needs live data — failing")
            return JSONResponse({"report": "Could not retrieve data for this topic. Please try again.", "charts": [], "keyStats": [], "summary": "", "title": ""})
        log.info("Report: no web sources found, but question doesn't require live data — "
                  "generating report from model knowledge instead of failing")

    # ── Verified index data: scraped sources are hit-or-miss on precise
    # Nifty/Sensex/Bank Nifty levels (stale snippets, wrong page, etc.), so
    # for any question that clearly concerns index levels we fetch real
    # quotes and prepend them as an authoritative source the model is told
    # to prefer over anything else for those exact numbers. ──
    _is_index_question = bool(re.search(
        r"\b(nifty|sensex|bse|nse|bank nifty|index|indices|"
        r"indian stock market|indian equity|indian share market|"
        r"sector rotation|stock markets? in india)\b",
        question, re.IGNORECASE,
    ))
    if _is_index_question:
        try:
            quotes = await fetch_index_quotes()
            quote_source = format_quotes_as_source(quotes)
            if quote_source:
                sources = [quote_source] + sources
                log.info("Report: prepended verified index quotes (%d symbols)", len(quotes))
        except Exception as e:
            log.warning("Report: index quote fetch failed, continuing without it: %s", e)

        # A question that ALSO carries historical/quarter-over-quarter intent
        # (the same signal _augment_query_for_historical_data already used to
        # steer the Tavily query) needs a real closing-price series, not just
        # today's snapshot — Tavily search can't reliably surface a clean
        # multi-quarter data table (see: the sector-rotation report that fell
        # back to generic business-cycle theory instead of actual numbers).
        # Fetch it directly and prepend as its own authoritative source.
        if _HISTORICAL_INTENT_RE.search(question):
            try:
                hist_series = await fetch_historical_index_quotes()
                hist_source = format_historical_quotes_as_source(hist_series)
                if hist_source:
                    sources = [hist_source] + sources
                    log.info("Report: prepended verified historical index series (%d symbols)", len(hist_series))
            except Exception as e:
                log.warning("Report: historical index fetch failed, continuing without it: %s", e)

    # Fetch real page content (not just Tavily's snippet) for as many sources
    # as we reasonably can — this is where the actual chartable numbers live.
    # Tavily's fan-out routinely returns ~30 sources; we used to discard
    # everything past 20 and only real-fetch the first 15 of those. Raised to
    # use 25 sources and real-fetch all of them — the model's context window
    # has plenty of headroom, and more real page text = more genuine data
    # points to chart/table instead of the same handful of numbers reused.
    ENRICH_SOURCE_COUNT = 25
    ENRICH_FETCH_CHARS = 3000

    async def enrich(src: dict, idx: int) -> dict:
        if src.get("url", "").startswith("internal://"):
            return src
        if _looks_like_ai_overview(src.get("snippet", "")):
            src = {**src, "snippet": ""}
        if _looks_like_ai_overview(src.get("fullContent", "")):
            src = {**src, "fullContent": ""}
        if len(src.get("fullContent", "")) > 600:
            return src
        fetched = await fetch_page_content(src["url"], ENRICH_FETCH_CHARS)
        if _looks_like_ai_overview(fetched):
            return src
        if len(fetched) > len(src.get("snippet", "")):
            log.debug("Enriched source %d: %s (+%d chars)", idx + 1, src.get("title", "")[:40], len(fetched))
            return {**src, "fullContent": fetched}
        return src

    log.info("Report: enriching sources...")
    enriched = list(await asyncio.gather(
        *[enrich(s, i) for i, s in enumerate(sources[:ENRICH_SOURCE_COUNT])]
    ))
    log.info("Report: enrichment done (%d sources ready)", len(enriched))

    src_text = "\n\n---\n\n".join(
        f"- **{s['title']}**\nSource: {s['url']}\n"
        + (s["fullContent"][:ENRICH_FETCH_CHARS] if len(s.get("fullContent", "")) > len(s.get("snippet", "")) else s.get("snippet", "")[:1000])
        for s in enriched
    )

    # ── OKF structured context from Supabase ─────────────────────────────────
    # When files were indexed via Paperly, their OKF concept files (markdown
    # with YAML frontmatter) were written to Supabase Storage under
    # paperly-okf/<session_id>/. Fetching them here gives the report LLM a
    # structured, typed view of each source document — much cleaner than the
    # raw chunked text that comes back from FAISS retrieval, which improves
    # title accuracy, section attribution, and factual grounding.
    okf_context = ""
    if session_id and has_file_data:
        try:
            # [File: name] markers in file_context come from files actually
            # attached THIS turn (see buildFilePayload on the frontend) — use
            # them to scope OKF context to the current request rather than
            # every document ever uploaded under this (long-lived) session id.
            current_filenames = set(re.findall(r"\[File:\s*([^\]]+?)\]", file_context))
            okf_context = await _fetch_okf_context(session_id, current_filenames=current_filenames)
            if okf_context:
                log.info("Report: injecting OKF structured context (%d chars) for session %s",
                         len(okf_context), session_id[:8])
        except Exception as _exc:
            log.warning("Report: OKF context fetch failed (non-fatal): %s", _exc)

    # ── Build user prompt — file content takes priority ───────────────────────
    file_section = ""
    if extracted_image_context:
        file_section += f"\n\n━━ DATA EXTRACTED FROM UPLOADED FILE IMAGES ━━\n{extracted_image_context[:8000]}\n━━ END FILE IMAGE DATA ━━\n"
    elif file_images:
        # Vision extraction was attempted (file_images is non-empty) but came
        # back with nothing usable — i.e. any tables/charts that exist ONLY
        # as images in the source (not as selectable text) were NOT read.
        # Without this explicit signal, a gap here can otherwise get quietly
        # filled with the model's own background knowledge of the subject —
        # see the NO BACKGROUND-KNOWLEDGE FILL-IN rule above.
        file_section += (
            "\n\n━━ NOTE: IMAGE-BASED DATA UNAVAILABLE ━━\n"
            "The uploaded file's page images could not be analysed this time (vision extraction "
            "failed). Any tables/charts/figures that exist ONLY as pictures in the source (not as "
            "selectable text below or in the structured context) are NOT available for this report. "
            "Do not reconstruct or estimate their contents — omit that specific table/data point or "
            "state it is not available from sources.\n"
            "━━ END NOTE ━━\n"
        )
    if okf_context:
        file_section += f"\n\n━━ STRUCTURED SOURCE DOCUMENTS (OKF) ━━\nEach block below is one uploaded file, structured as an Open Knowledge Format concept.\nUse these as your PRIMARY source — they are typed, titled, and extracted from the actual files the user uploaded.\n\n{okf_context}\n━━ END OKF SOURCES ━━\n"
    if file_context.strip():
        # 6000 chars used to be the ceiling here, which is fine for a page or
        # two of prose but silently guillotines a multi-sheet spreadsheet
        # (e.g. a PMS/fund performance workbook with 20+ sheets) down to a
        # fraction of its first sheet. Gemini's context window comfortably
        # fits this; Groq's own pre-flight size check (GROQ_MAX_PROMPT_CHARS,
        # see call_groq) already skips straight to Gemini when the combined
        # prompt is too large, so raising this is safe on both paths.
        file_section += f"\n\n━━ UPLOADED FILE TEXT CONTENT ━━\n{file_context[:150000]}\n━━ END FILE CONTENT ━━\n"

    # Image placement instruction — tell LLM where to place extracted
    # chart/figure image references. Deliberately built from
    # embedded_file_images (real embedded images pulled out of the PDF), NOT
    # file_images (full-page renders) — we never want a whole page screenshot
    # embedded in the report. If the uploaded file had no embeddable images
    # (e.g. its charts/tables are drawn as vector shapes, not raster images),
    # this list is empty and no [FILE_IMG_n] instruction is given at all.
    img_placement_instruction = ""
    if embedded_file_images:
        img_list = ", ".join(f"[FILE_IMG_{i+1}] = \"{img['name']}\"" for i, img in enumerate(embedded_file_images[:8]))
        img_placement_instruction = (
            f"\n\nIMAGE PLACEMENT RULES:\n"
            f"The following images/charts/figures were extracted from the uploaded file: {img_list}\n"
            f"These are actual embedded images from the file (charts, graphs, photos) — NOT full page screenshots. "
            f"When discussing data that one of these images visualises, insert [FILE_IMG_n] "
            f"on its own line right after the relevant paragraph. The PDF renderer will embed the actual image there. "
            f"Only place [FILE_IMG_n] where it genuinely adds context — do NOT place all of them, only the most relevant ones (max 4)."
        )

    # Prior conversation turns, if this report follows on from a chat thread.
    # Kept deliberately separate from file_section above (which is framed as
    # PRIMARY SOURCE data) — this is background only, for continuity, and
    # must never become the report's subject or override the question below.
    conversation_section = ""
    if conversation_context.strip():
        conversation_section = (
            f"\n\nPRIOR CONVERSATION (background only — NOT a data source, "
            f"do NOT extract numbers from this or base the title/topic on it; "
            f"it exists only so a pronoun-heavy follow-up question makes sense):\n"
            f"{conversation_context[:2000]}\n"
        )

    # Curate the images Tavily found during the search(es) above into a
    # numbered candidate list the model can pick from (never URLs it invents
    # itself — see _validate_image_selections below).
    # Web-image embedding (stock/decorative photos from Tavily image search) is
    # disabled — the report should only carry charts, graphs and tables, never
    # unrelated stock photography. Forcing this to an empty list makes every
    # downstream helper (_force_fallback_images, _top_up_images,
    # _inject_fallback_image_placeholders) a no-op, since they all short-circuit
    # when image_candidates/images is empty.
    image_candidates: list[dict] = []
    image_candidates_block = ""

    from datetime import date
    _today_date = date.today()
    today = _today_date.strftime("%A, %B %d, %Y")
    quarter_anchor = _past_n_quarters_anchor(question, _today_date)

    user_prompt = (
        f"Today's date is {today}. Resolve \"latest\", \"current\", \"this quarter/year\", "
        f"\"past N quarters/months/years\", and any other relative time reference in the "
        f"question strictly against this date — never against your own training data or "
        f"internal sense of what the current date/quarter/year is. If the sources provided "
        f"below don't clearly establish which period is \"current\" as of {today}, say so "
        f"rather than guessing."
        + quarter_anchor
        + "\n\n"
        f"Research Question / Topic (this — and ONLY this — defines the report's title and subject): {question}\n"
        + (f"\nPRIMARY SOURCE — ANALYSE THIS FIRST (uploaded file data takes highest priority):{file_section}" if file_section else "")
        + conversation_section
        + (img_placement_instruction if img_placement_instruction else "")
        + (
            f"\n\nSupplementary web sources ({len(enriched)} results):\n\n{src_text}\n\n"
            if not no_web_sources else
            "\n\nNO WEB SOURCES: A web search for this topic returned nothing usable — this is "
            "expected for a personal/advisory question rather than a news or market-data query. "
            "Write the report entirely from your own financial/business knowledge and reasoning. "
            "Do NOT invent citations, publication names, statistics, or URLs you were not actually "
            "given — attribute nothing to a 'source' that doesn't exist here. Where a number is a "
            "reasonable estimate or industry rule-of-thumb rather than a verified figure, say so "
            "explicitly (e.g. 'typically', 'a common benchmark is', 'as a rough estimate'). Omit any "
            "References/Sources/Methodology section entirely, since there are no sources to list.\n\n"
        )
        + image_candidates_block
        + "INSTRUCTIONS:\n"
        "1. The uploaded file content (if provided) is your PRIMARY source — extract ALL numbers, tables, charts, and statistics from it first.\n"
        "2. Use web sources to supplement and validate the file data.\n"
        "3. Follow CHART RULES exactly — reproduce actual data from the file as charts where it exists.\n"
        "4. Write the full 6-section, long-form report (target 3500-4500 words (MINIMUM 22000 characters — shorter responses will be rejected and retried)). Insert [CHART_n] inline where valid chart data exists.\n"
        "5. Data → [CHART_n] or a table, always. Only if a section is genuinely non-numeric/thematic and would "
        "otherwise be plain text, you MAY add up to 2 AI-generated illustrative images total — see AI IMAGE RULES. "
        "Default to zero images; most reports should return \"images\": [].\n"
        + ("6. Insert [FILE_IMG_n] references inline where you reference data visible in that extracted image/chart.\n" if embedded_file_images else "")
        + (
            "7. THE USER EXPLICITLY ASKED FOR MORE DATA POINTS / CHARTS / GRAPHS — go beyond the usual "
            "STEP 4 floor of 6: produce AT LEAST 8-10 [CHART_n]/table entries if the source material "
            "(file data, web sources, or — when NO_WEB_SOURCES — figures/ratios you can validly derive "
            "from the numbers already given) supports that many distinct chartable angles. For every "
            "metric mentioned in the text, also surface it as a keyStats entry or a chart data point "
            "rather than leaving it as a bare sentence. Where the same underlying numbers support more "
            "than one lens (e.g. absolute values AND ratios/percentages, current-state AND trend-over-"
            "time, per-unit AND aggregate), chart more than one of those lenses instead of picking just "
            "one. This does NOT license inventing numbers — every extra chart/stat still must trace back "
            "to a real source figure or a straightforward derived calculation from figures already given "
            "(e.g. revenue ÷ client count = ARPU is fine; a number with no basis is not).\n"
            if wants_more_data_viz else ""
        )
        + "8. Respond ONLY with the JSON object — no markdown fences, no text outside JSON."
    )

    raw, model_used = await call_gemini(user_prompt)
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

    # ── Pre-parse JSON repair ──────────────────────────────────────────────────────────────
    # The LLM occasionally emits unescaped double-quotes inside string values
    # (most often "report", since it's long-form prose that frequently quotes
    # analysts, ratings like "buy"/"sell", etc.), causing json.loads to fail
    # mid-way. We scan char-by-char through each value to find and fix them.
    #
    # IMPORTANT: the old heuristic decided a bare `"` was the value's
    # *closing* quote just because the very next non-space character was a
    # comma/brace/bracket. That's wrong — an internal quote like
    # `rate it "buy",` also satisfies that check, since prose is full of
    # quoted phrases immediately followed by a comma. Stopping there means
    # everything after that false-positive point is copied through
    # unescaped, which corrupts the rest of the JSON (charts/images/keyStats
    # end up mangled or swallowed into the string) — this was the root cause
    # of reports coming back with no charts and a report body that looked
    # cut short. We now only treat a `"` as the real closing quote when what
    # follows is unambiguous JSON structure: end-of-object, end-of-text, or
    # a comma that leads directly into one of this schema's known next keys
    # — not just "any comma".
    _NEXT_FIELD_RE = re.compile(r'^\s*,\s*"(title|report|charts|images|keyStats|summary)"\s*:')
    _OBJ_CLOSE_RE = re.compile(r'^\s*\}')
    # Handles the case where Gemini drops BOTH the closing quote of the
    # current field AND the comma before the next key, so a single quote
    # character ends up doing double duty as the closing delimiter of this
    # value and the opening delimiter of the next key's string
    # (e.g. `"title": "Q3 Report — Deep Dive"report": "..."`, no `,` and
    # no second `"`). _NEXT_FIELD_RE requires a leading comma so it never
    # matches this shape, which is why the scanner used to treat that quote
    # as internal and kept escaping/consuming everything after it —
    # swallowing charts/images/keyStats into the title string and leaving
    # the JSON unterminated.
    _NEXT_FIELD_SHARED_QUOTE_RE = re.compile(r'^(title|report|charts|images|keyStats|summary)"\s*:')

    def _repair_string_value(text: str, field: str) -> str:
        """Find "field": "<value>" and escape any bare internal double-quotes."""
        key_pat = re.compile(r'"' + re.escape(field) + r'"\s*:\s*"')
        m = key_pat.search(text)
        if not m:
            return text
        val_start = m.end()        # index of first char after the opening quote
        out = list(text[:val_start])
        i = val_start
        n = len(text)
        _VALID_ESCAPES = set('"\\/bfnrtu')
        while i < n:
            ch = text[i]
            if ch == '\\':
                nxt = text[i + 1] if i + 1 < n else ''
                if nxt in _VALID_ESCAPES:
                    # Genuine JSON escape sequence (\", \\, \n, \uXXXX, etc.) — copy verbatim.
                    out.append(ch)
                    i += 1
                    if i < n:
                        out.append(text[i])
                        i += 1
                    continue
                # Not a valid JSON escape — the model emitted a stray backslash
                # (e.g. "43\%" or "Rs.\1,000" from percentages, fractions, or
                # footnote-style markup). json.loads rejects any "\<char>" that
                # isn't one of the fixed JSON escapes, with "Invalid \escape".
                # Escape the backslash itself so it becomes a literal backslash
                # character in the parsed string, and let the following
                # character be processed normally on the next loop iteration
                # (it might itself be a quote that needs the logic below).
                out.append('\\')
                out.append('\\')
                i += 1
                continue
            if ch == '"':             # unescaped quote — closing or internal?
                rest = text[i + 1:i + 300]
                if _OBJ_CLOSE_RE.match(rest) or _NEXT_FIELD_RE.match(rest) or rest.strip() == '':
                    # Genuine closing delimiter — emit and keep the rest verbatim
                    out.append(ch)
                    out.append(text[i + 1:])
                    return ''.join(out)
                m_shared = _NEXT_FIELD_SHARED_QUOTE_RE.match(rest)
                if m_shared:
                    # This quote is the model's dropped closing delimiter,
                    # fused directly onto the next key with no comma.
                    # Split it back into close-quote + comma + open-quote
                    # so the next key parses as its own field instead of
                    # being swallowed into this string.
                    field_start_abs = i + 1 + m_shared.start(1)
                    out.append('"')
                    out.append(', "')
                    out.append(text[field_start_abs:])
                    return ''.join(out)
                # Internal bare quote — escape it and keep scanning
                out.append('\\')
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    # Repair every top-level string field, in schema order — title first,
    # since a broken title corrupts everything that comes after it too.
    for _field in ("title", "report", "summary"):
        clean = _repair_string_value(clean, _field)

    def _fix_invalid_json_escapes(text: str) -> str:
        """Defense-in-depth beyond _repair_string_value above: escape any
        backslash anywhere in the JSON text that isn't part of a valid JSON
        escape sequence (\\", \\\\, \\/, \\b, \\f, \\n, \\r, \\t, \\uXXXX).
        The model occasionally emits stray backslashes outside the
        title/report/summary fields too — e.g. a chart title or keyStat
        label containing "43\\%" or "Rs.\\1,000" — which json.loads rejects
        with "Invalid \\escape". Safe to run on the whole text: backslashes
        only ever legitimately appear inside JSON string values anyway.
        """
        out = []
        i, n = 0, len(text)
        valid = set('"\\/bfnrtu')
        while i < n:
            ch = text[i]
            if ch == '\\':
                nxt = text[i + 1] if i + 1 < n else ''
                if nxt in valid:
                    out.append(ch); i += 1
                    if i < n:
                        out.append(text[i]); i += 1
                    continue
                out.append('\\'); out.append('\\')
                i += 1
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    clean = _fix_invalid_json_escapes(clean)

    def _extract_balanced_array(text: str, key: str) -> str | None:
        """Find "key": [ ... ] and return the FULL bracket-balanced array
        text, correctly handling nested arrays/objects (e.g. a chart's
        "series": [...] inside "charts": [...]). The old approach used a
        non-greedy regex (\\[[\\s\\S]*?\\]) that stops at the FIRST closing
        bracket it sees — which is almost always a nested one, not the
        outer array's — producing a truncated fragment that fails to parse
        and gets silently dropped. That's why charts/keyStats often came
        back empty even when the model had generated them."""
        m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', text)
        if not m:
            return None
        start = m.end() - 1  # index of the opening '['
        depth = 0
        in_str = False
        esc = False
        i = start
        n = len(text)
        while i < n:
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
            i += 1
        return None  # never closed — genuinely truncated, caller should skip
    # ─────────────────────────────────────────────────────────────────────────

    def _num(pt: dict) -> float:
        """Safely coerce a chart point's value to a number. pt.get("value", 0)
        only falls back to 0 when the key is *missing* — if the model emits
        an explicit `"value": null`, .get returns None and later numeric
        comparisons (e.g. `v > 0`) crash with a TypeError. Treat None/missing
        the same way."""
        v = pt.get("value", 0)
        return v if isinstance(v, (int, float)) else 0

    def _is_plausible_chart(ch: dict) -> bool:
        if ch.get("type") == "table":
            cols = ch.get("columns") or []
            rows = ch.get("rows") or []
            return bool(ch.get("title")) and len(cols) >= 2 and len(rows) >= 2
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

        # Enforce a minimum-distinct-items rule for bar/pie charts (matches
        # the system prompt's own STEP 2 rule). A single-series bar chart
        # with only 2 bars (e.g. just "HDFC Bank" vs "ICICI Bank") reads as
        # a thin, low-value visual — reject it so the model either pulls in
        # more comparable entities from the sources or skips the chart.
        # Pie charts are allowed down to 2 slices (e.g. "FII vs DII flows",
        # "green vs red IPO listings") since a 2-way split is still a
        # legitimate, common pie — unlike a 2-bar chart it isn't thin, it's
        # just binary.
        if chart_type in ("bar", "pie") and n_series == 1:
            n_labels = len(series[0].get("data") or [])
            min_labels = 2 if chart_type == "pie" else 3
            if chart_type == "bar" and n_labels == 2:
                vals = [_num(pt) for pt in series[0].get("data") or []]
                is_diverging = len(vals) == 2 and (vals[0] > 0) != (vals[1] > 0)
                if is_diverging:
                    # e.g. FII outflow (-735) vs DII inflow (+705) — a genuine
                    # 2-way diverging comparison, not a "thin" chart. Can't be
                    # a pie (negative values aren't representable as slices),
                    # so it's allowed through as a bar despite the usual ≥3 rule.
                    min_labels = 2
            if n_labels < min_labels:
                log.warning("Chart rejected — only %d distinct items (need ≥%d for %s chart): %s",
                            n_labels, min_labels, chart_type, ch.get("title", "?"))
                return False

        # Arrow/scatter charts share the two-series-sharing-labels shape, but
        # need their own minimum-items rule (matches STEP 2 in the system
        # prompt): an arrow chart with 1 item is just "before/after" prose
        # wearing a chart, and a scatter plot with under 4 points is a
        # handful of dots with no visible relationship to show.
        if chart_type in ("arrow", "scatter"):
            n_labels = len(series[0].get("data") or []) if series else 0
            min_labels = 2 if chart_type == "arrow" else 4
            if n_labels < min_labels:
                log.warning("Chart rejected — only %d distinct items (need ≥%d for %s chart): %s",
                            n_labels, min_labels, chart_type, ch.get("title", "?"))
                return False

        all_pts = [pt for s in series for pt in (s.get("data") or [])]
        values  = [_num(pt) for pt in all_pts]
        if len(values) > 1 and len(set(values)) <= 1:
            log.warning("Chart rejected — identical values: %s", values[:6])
            return False

        # Reject bar charts where a single series mixes wildly different scales
        # (e.g. price 125957 and % change -3 as two bars in the same series)
        if chart_type == "bar":
            for s in series:
                pts_vals = [_num(pt) for pt in (s.get("data") or [])]
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

    def _chart_signature(ch: dict) -> tuple:
        """A hashable fingerprint of a chart's actual content (type + title +
        every series' label/value pairs), used to catch the model emitting
        the *same* chart twice (identical data, occasionally even an
        identical title) under two separate [CHART_n] placeholders — which
        renders as two visually-identical cards back to back in the PDF.
        Table charts are excluded (tables already have their own dedup via
        `_table_duplicates_existing_chart` / `_seen_table_sigs`)."""
        if ch.get("type") == "table":
            return ()
        series_sig = tuple(
            (s.get("name", ""), tuple((str(p.get("label", "")), p.get("value")) for p in (s.get("data") or [])))
            for s in (ch.get("series") or [])
        )
        return (ch.get("type", ""), (ch.get("title") or "").strip().lower(), series_sig)

    def _drop_duplicate_charts(mask: list[bool], chart_list: list[dict]) -> list[bool]:
        """Given an existing plausibility mask, additionally turn off any
        chart whose content signature exactly matches an earlier chart
        that's still kept — so a repeated chart is dropped the same way an
        implausible one is (and [CHART_n] placeholders get renumbered
        around it by the existing `_remap_chart_placeholders` call)."""
        seen: set[tuple] = set()
        out = list(mask)
        for i, ch in enumerate(chart_list):
            if not out[i]:
                continue
            sig = _chart_signature(ch)
            if not sig:  # tables — handled elsewhere
                continue
            if sig in seen:
                log.info("Chart rejected — exact duplicate of an earlier chart: %s", ch.get("title", "?"))
                out[i] = False
            else:
                seen.add(sig)
        return out

    def _strip_url_columns(chart_list: list[dict]) -> list[dict]:
        """Belt-and-braces twin of the URL-column stripping already done in
        `_extract_markdown_tables` (see there for the ambiguous-header
        heuristic) — applied here too because a "table" chart can also
        arrive as a JSON object straight in the model's own "charts" array
        (never went through markdown parsing at all), and reports never
        show raw URLs anywhere, regardless of which path a table took."""
        unambiguous = {"url", "link", "source url", "web link", "hyperlink"}
        ambiguous = {"source", "website"}
        for ch in chart_list:
            if ch.get("type") != "table":
                continue
            cols = ch.get("columns") or []
            rows = ch.get("rows") or []
            if not cols:
                continue

            def _is_url_col(idx: int, header: str) -> bool:
                h = str(header).strip().lower()
                if h in unambiguous:
                    return True
                if h in ambiguous:
                    vals = [str(r[idx]).strip() for r in rows if idx < len(r) and str(r[idx]).strip()]
                    if not vals:
                        return False
                    return sum(1 for v in vals if re.match(r"^https?://", v)) >= max(1, len(vals) * 0.6)
                return False

            drop = {idx for idx, h in enumerate(cols) if _is_url_col(idx, h)}
            if drop and len(cols) - len(drop) >= 2:
                ch["columns"] = [c for idx, c in enumerate(cols) if idx not in drop]
                ch["rows"] = [[c for idx, c in enumerate(r) if idx not in drop] for r in rows]
        return chart_list

    def _recover_thin_bar_as_pie(ch: dict) -> dict:
        """A single-series bar chart with exactly 2 items fails the ≥3-item
        bar rule but is a perfectly legitimate pie IF both values are a genuine
        non-negative share-of-whole (e.g. "IPO listings: green vs red" 12 vs 8).
        A pie can't represent a negative value, so signed/diverging pairs (e.g.
        FII outflow -735 vs DII inflow +705) are deliberately left as bars —
        the validator's diverging-bar rule allows those through directly."""
        if ch.get("type") != "bar":
            return ch
        series = ch.get("series") or []
        if len(series) == 1 and len(series[0].get("data") or []) == 2:
            vals = [_num(pt) for pt in series[0].get("data") or []]
            if all(v >= 0 for v in vals):
                log.info("Chart auto-converted bar→pie (2 items): %s", ch.get("title", "?"))
                return {**ch, "type": "pie"}
        return ch

    _RELATIVE_PERIOD_RE = re.compile(
        r"\b(previous|past|last|recent|current)\s+(session|day|week|month|quarter|year)\b|\btoday\b",
        re.IGNORECASE,
    )

    def _recover_pseudo_trend_line_as_bar(ch: dict) -> dict:
        """A single-series line chart is only a real 'trend' if it has several
        points along an actual chronological axis. Two failure patterns show
        up otherwise: (1) exactly 2 data points stretched across a dense
        multi-week date axis, implying daily granularity that doesn't exist
        (e.g. a gold-reserves figure known only for Q4 and Q1, drawn as a
        smooth line across 15 weekly gridlines); (2) labels that are
        heterogeneous relative-period phrases — "Previous Session", "Past
        Month", "Past Year" — plotted as if they were sequential time points,
        which draws a misleading trend line out of unrelated stats. Both are
        really a discrete comparison, not a time series — convert to bar."""
        if ch.get("type") != "line":
            return ch
        series = ch.get("series") or []
        if len(series) != 1:
            return ch  # multi-series comparisons (e.g. Gold vs Silver) are fine as-is
        data = series[0].get("data") or []
        labels = [str(pt.get("label", "")) for pt in data]
        too_few_points = len(data) == 2
        heterogeneous_labels = any(_RELATIVE_PERIOD_RE.search(l) for l in labels)
        if too_few_points or heterogeneous_labels:
            log.info("Chart auto-converted line→bar (pseudo-trend): %s", ch.get("title", "?"))
            return {**ch, "type": "bar"}
        return ch

    try:
        parsed = json.loads(clean)

        original_charts_list = [
            _recover_pseudo_trend_line_as_bar(_recover_thin_bar_as_pie(c))
            for c in (parsed.get("charts") or [])
        ]
        valid_mask = [_is_plausible_chart(c) for c in original_charts_list]
        valid_mask = _drop_duplicate_charts(valid_mask, original_charts_list)
        charts = [c for c, keep in zip(original_charts_list, valid_mask) if keep]
        if len(charts) < len(original_charts_list):
            log.info("Chart validation: kept %d / %d charts", len(charts), len(original_charts_list))

        report_text = parsed.get("report", "")
        report_text = _strip_leaked_prompt_tail(report_text)
        # Renumber/strip [CHART_n] placeholders so they still point at the
        # right chart now that some may have been dropped above — otherwise
        # every placeholder after a rejected chart points one slot too far
        # into the now-shorter array (see _remap_chart_placeholders docstring).
        report_text = _remap_chart_placeholders(report_text, original_charts_list, valid_mask)

        original_images_list = parsed.get("images") or []
        images, images_valid_mask = await _generate_ai_report_images(original_images_list)
        if len(images) < len(original_images_list):
            log.info("AI image generation: produced %d / %d requested", len(images), len(original_images_list))

        # ── Deterministic safety net ──────────────────────────────────────
        # The model is asked to pick 2-4 relevant images (see IMAGE RULES),
        # but after many rounds of it punting to [] even with decent
        # candidates available, don't make "is there a photo in this report"
        # depend entirely on the model feeling confident enough to comply.
        images, was_forced = _force_fallback_images(images, image_candidates, model_used)
        if was_forced:
            images_valid_mask = [True] * len(images)
        elif not image_candidates:
            log.info("Report: no image candidates were available for this query — report will have 0 images")
        report_text = _remap_web_image_placeholders(report_text, images_valid_mask)

        if "\\n" in report_text:
            report_text = report_text.replace("\\n", "\n")
        report_text = re.sub(r"^```(?:json|markdown)?\s*", "", report_text.strip())
        report_text = re.sub(r"```\s*$", "", report_text).strip()
        # Strip raw JSON chart blobs the LLM leaked into the report body and
        # convert each into a [CHART_n] placeholder so it still renders.
        report_text, charts = _extract_inline_chart_jsons(report_text, charts)
        # Turn markdown data tables into Datawrapper table charts too.
        report_text, charts = _extract_markdown_tables(report_text, charts)

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

        charts = _strip_url_columns(charts)
        charts = await attach_datawrapper_charts(charts)
        report_text = _strip_citation_markers(report_text)
        # Debug: verify [WEB_IMG_n] placeholders are in final report_text
        import re as _re_dbg2
        _wimg_ph = _re_dbg2.findall(r"\[WEB_IMG_\d+\]", report_text)
        log.info("Report: final [WEB_IMG] placeholders in text: %s  images list len: %d", _wimg_ph or "none", len(images))

        # ── Top up: rules ask for 2-4 images, but the model has sometimes
        # selected just 1 even with more decent candidates left unused — don't
        # leave the report thinner on photos than what was actually available.
        images, _ = _top_up_images(images, image_candidates, model_used)

        # ── Fallback: inject a [WEB_IMG_n] marker for any image (model-picked,
        # forced, or topped-up) that doesn't already have one in the body, so
        # it actually renders (both in the UI and in the PDF).
        _report_text_before_inject = report_text
        report_text = _inject_fallback_image_placeholders(report_text, images)
        if report_text != _report_text_before_inject:
            log.info("Report: injected fallback [WEB_IMG_n] placeholder(s) — %d image(s) total now placed", len(images))
        clean_title = _sanitize_title(parsed.get("title", ""), question)
        elapsed = (time.perf_counter() - t0) * 1000
        log.info("Report complete in %.0fms — title=%r  charts=%d  images=%d  keyStats=%d",
                 elapsed, clean_title[:60], len(charts), len(images), len(parsed.get("keyStats", [])))
        return JSONResponse({
            "title":      clean_title,
            "report":     report_text,
            "charts":     charts,
            "images":     images,  # validated {url, caption} pairs for PDF/UI embedding
            "keyStats":   parsed.get("keyStats", []),
            "summary":    parsed.get("summary", ""),
            "fileImages": embedded_file_images,  # extracted charts/images only — never full pages
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
                # Only chop back to the last clean section boundary if the
                # match actually ran off the end of the text without hitting
                # a real closing quote (i.e. genuinely truncated output).
                # Since "report" was already quote-repaired above, a match
                # that stops well short of len(text) found a real closing
                # delimiter — that's a complete field and shouldn't be cut.
                genuinely_truncated = m.end(1) >= len(text) - 2
                if genuinely_truncated:
                    for boundary in ["\n## ", "\n### ", "\n\n"]:
                        last = report_raw.rfind(boundary)
                        if last > len(report_raw) * 0.4:
                            report_raw = report_raw[:last].strip()
                            break
                report_raw = _strip_leaked_prompt_tail(report_raw)
                if len(report_raw) > 200:
                    result["report"] = report_raw
            arr_text = _extract_balanced_array(text, "keyStats")
            if arr_text:
                try:
                    result["keyStats"] = json.loads(arr_text)
                except Exception as e:
                    log.debug("Report salvage: keyStats array failed to parse (%s)", e)
            arr_text = _extract_balanced_array(text, "charts")
            if arr_text:
                try:
                    result["charts"] = json.loads(arr_text)
                except Exception as e:
                    log.debug("Report salvage: charts array failed to parse (%s)", e)
            # Also try to salvage images array from truncated JSON
            arr_text = _extract_balanced_array(text, "images")
            if arr_text:
                try:
                    raw_imgs = json.loads(arr_text)
                    if image_candidates and raw_imgs:
                        validated, _ = _validate_image_selections(raw_imgs, image_candidates)
                        result["images"] = validated
                except Exception as e:
                    log.debug("Report salvage: images array failed to parse/validate (%s)", e)
            return result if result.get("report") else None

        salvaged = _try_extract_fields(clean)
        if salvaged:
            salvaged["images"], _ = _force_fallback_images(salvaged.get("images") or [], image_candidates, model_used)
            salvaged["images"], _ = _top_up_images(salvaged["images"], image_candidates, model_used)
            salvaged["report"] = _inject_fallback_image_placeholders(salvaged.get("report", ""), salvaged["images"])
            log.info("Report: salvaged %d fields from truncated JSON (images=%d)", len(salvaged), len(salvaged.get("images", [])))
            # Same chart pipeline as the happy path: validate whatever the model
            # put in "charts", remap [CHART_n] placeholders around any that get
            # dropped, then also pull in any raw chart JSON or markdown tables
            # the model left directly in the report body. Truncated JSON was a
            # common reason PDFs came back with zero charts even though the
            # model had actually generated good ones (or good table data) —
            # skipping this pipeline on the salvage path silently threw them away.
            _raw_charts = [
                _recover_pseudo_trend_line_as_bar(_recover_thin_bar_as_pie(c))
                for c in (salvaged.get("charts") or [])
            ]
            _valid_mask = [_is_plausible_chart(c) for c in _raw_charts]
            _valid_mask = _drop_duplicate_charts(_valid_mask, _raw_charts)
            _salv_charts = [c for c, keep in zip(_raw_charts, _valid_mask) if keep]
            if len(_salv_charts) < len(_raw_charts):
                log.info("Chart validation (salvage): kept %d / %d charts", len(_salv_charts), len(_raw_charts))
            salvaged["report"] = _remap_chart_placeholders(salvaged.get("report", ""), _raw_charts, _valid_mask)
            salvaged["report"], _salv_charts = _extract_inline_chart_jsons(salvaged["report"], _salv_charts)
            salvaged["report"], _salv_charts = _extract_markdown_tables(salvaged["report"], _salv_charts)
            _salv_charts = _strip_url_columns(_salv_charts)
            salvaged["charts"] = await attach_datawrapper_charts(_salv_charts)
            return JSONResponse({
                "title":      _sanitize_title(salvaged.get("title", ""), question),
                "report":     _strip_citation_markers(salvaged.get("report", "")),
                "charts":     salvaged.get("charts", []),
                "images":     salvaged.get("images", []),
                "keyStats":   salvaged.get("keyStats", []),
                "summary":    salvaged.get("summary", ""),
                "fileImages": embedded_file_images,
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
            repaired_report = (repaired_parsed.get("report", "") or "").replace("\\n", "\n")
            repaired_report = _strip_leaked_prompt_tail(repaired_report)
            repaired_imgs = []
            try:
                raw_imgs = repaired_parsed.get("images") or []
                if image_candidates and raw_imgs:
                    repaired_imgs, _ = _validate_image_selections(raw_imgs, image_candidates)
            except Exception as e:
                log.debug("Report repair: image validation failed, continuing without images (%s)", e)
            repaired_imgs, _ = _force_fallback_images(repaired_imgs, image_candidates, model_used)
            repaired_imgs, _ = _top_up_images(repaired_imgs, image_candidates, model_used)
            _raw_charts = [
                _recover_pseudo_trend_line_as_bar(_recover_thin_bar_as_pie(c))
                for c in (repaired_parsed.get("charts") or [])
            ]
            _valid_mask = [_is_plausible_chart(c) for c in _raw_charts]
            _valid_mask = _drop_duplicate_charts(_valid_mask, _raw_charts)
            _rep_charts = [c for c, keep in zip(_raw_charts, _valid_mask) if keep]
            if len(_rep_charts) < len(_raw_charts):
                log.info("Chart validation (repair): kept %d / %d charts", len(_rep_charts), len(_raw_charts))
            repaired_report = _remap_chart_placeholders(repaired_report, _raw_charts, _valid_mask)
            repaired_report, _rep_charts = _extract_inline_chart_jsons(repaired_report, _rep_charts)
            repaired_report, _rep_charts = _extract_markdown_tables(repaired_report, _rep_charts)
            _rep_charts = _strip_url_columns(_rep_charts)
            repaired_charts = await attach_datawrapper_charts(_rep_charts)
            repaired_report = _inject_fallback_image_placeholders(repaired_report, repaired_imgs)
            repaired_report = _strip_citation_markers(repaired_report)
            return JSONResponse({
                "title":      _sanitize_title(repaired_parsed.get("title", ""), question),
                "report":     repaired_report,
                "charts":     repaired_charts,
                "images":     repaired_imgs,
                "keyStats":   repaired_parsed.get("keyStats", []),
                "summary":    repaired_parsed.get("summary", ""),
                "fileImages": embedded_file_images,
            })
        except Exception as e:
            log.warning("Report: JSON repair attempt failed (%s)", e)

        log.error("Report: could not salvage JSON — returning error message")
        return JSONResponse({
            "title": _sanitize_title("", question),
            "report": "## Report Generation Error\n\nThe AI response could not be parsed. Please try a more specific question.",
            "charts": [], "images": [], "keyStats": [], "summary": "", "fileImages": embedded_file_images,
        })

