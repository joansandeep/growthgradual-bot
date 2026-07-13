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

router = APIRouter()
log = logging.getLogger("report")

# ── OKF context fetcher ────────────────────────────────────────────────────────
import os as _os
_SB_URL = _os.environ.get("SUPABASE_URL", "").rstrip("/")
_SB_KEY = _os.environ.get("SUPABASE_ANON_KEY", "")
_OKF_BUCKET = "paperly-uploads"
_OKF_PREFIX = "paperly-okf"


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
  "report": "<full markdown report — target 2800-3500 words (MINIMUM 18000 characters — shorter responses will be rejected and retried), structured and data-rich. This is a LONG-FORM report (aim for ~10-12 printed pages once charts/tables/images are laid in) — see REPORT STRUCTURE below for the section list that gets you there. Add DEPTH, not invented data: more context, more explanation of mechanisms and causes, more comparison and discussion of what the numbers mean — never pad with filler sentences or numbers not in the sources.>",
  "charts": [...],
  "images": [...],
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

STEP 1 — AGGRESSIVELY SCAN sources for ANY chartable numbers:
  • Returns/performance of multiple funds, stocks, sectors → bar chart
  • Rankings with numbers (top 5 SIPs by return, top gainers) → bar chart
  • Time-series: quarterly results, monthly data, weekly prices → line chart
  • Allocation/composition (sector weights, portfolio mix) → pie chart
  • Comparisons: 1yr vs 3yr vs 5yr returns of same fund → bar chart
  • FII/DII flows by date → line or bar chart
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
  ✓ At least 3 data points (bar/pie) or 4 time points (line)
  ✓ All labels are DIFFERENT from each other
  ✓ All values are DIFFERENT from each other (not all the same)
  ✓ Values come from the source data — do NOT invent numbers
  ✗ NEVER create a chart from a single number
  ✗ NEVER duplicate labels
  ✗ NEVER use future/projected values you invented
  ✗ A bar chart needs ≥3 named distinct items; a pie chart needs ≥2 — this is
    enforced server-side and anything short of that WILL be silently dropped,
    wasting the slot.
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

STEP 3 — Place [CHART_n] inline in the report markdown right after the paragraph AND bullet list whose data it shows.
  charts[0] = [CHART_1], charts[1] = [CHART_2], etc.
  IMPORTANT: Never place [CHART_n] immediately after just 1-2 sentences — always ensure at least one full
  paragraph (3+ sentences) or a paragraph + bullet list precedes the chart. This prevents blank whitespace
  gaps in the PDF.

STEP 4 — Aim for 5-8 charts/tables per report when data supports it — this report runs long
  (10-12 pages), and visuals are what fill that length well rather than walls of text. Quality
  over quantity, but lean toward MORE high-level visuals rather than fewer when the sources have
  the numbers for it. Vary the shapes (bar, stacked bar, line, pie) rather than repeating the same
  shape for every chart; use the stacked-bar shape above whenever a breakdown is compared across
  multiple labels. If sources genuinely lack numeric data → 0 charts is acceptable. But look hard first.

Chart spec shape:
{
  "type": "bar" | "line" | "pie",
  "title": "<specific title e.g. 'Top 5 SIP Funds — 3-Year Returns' not 'Chart 1'>",
  "unit": "%" | "₹" | "Cr" | "B" | "$" | "x" | "",
  "stacked": true | false,  // ONLY for multi-series bar charts where each series is
                            // a PART of a whole per label (e.g. Equity/Debt/Cash per
                            // fund) — set true so it renders as one stacked column
                            // per label instead of side-by-side grouped bars. Omit
                            // or set false for comparison charts (entity vs entity).
  "series": [{ "name": "<series name>", "data": [{ "label": "<unique label>", "value": <number> }] }]
}

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
IMAGE RULES — this report is charts/graphs/tables only. Do NOT reference, request,
or place any stock/decorative photos. Never emit a [WEB_IMG_n] marker and never
return an "images" array — visual content in this report comes exclusively from
[CHART_n] (and [FILE_IMG_n] where applicable), not from web-sourced photography.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPORT STRUCTURE (each section MUST be substantive — this is a LONG-FORM report, ~10-12 pages
once charts/tables/images are laid in. Minimum word counts below are FLOORS, not targets —
write as much genuinely substantive analysis as the sources support):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# [Report Title]

## 1. Introduction
3-4 paragraphs (minimum 200 words): Provide rich context — explain the sector/topic, why it matters now, who the key stakeholders are, what macro or market forces are driving interest, the history/background that led here, and what this report covers. Write in plain prose. No filler, every sentence must add context or data.
Then add a **"What This Report Covers"** bullet list (4-6 short items, one per line, starting with "-") summarising the key questions this report answers — e.g. "- Which sectors led/lagged and by how much", "- Key macro drivers behind outperformers". This gives the reader a quick scannable preview.

## 2. Methodology
2-3 paragraphs (minimum 150 words): describe what sources were used, what metrics were collected, and how comparisons were made — the reader needs this BEFORE the Data Analysis section so they know the basis and scope of what follows. Explicitly state any caveats or limitations in the data (e.g. single-document scope, no external market figures used, time period covered). Do NOT include the sources table here — that belongs only in the final "Data Sources" section. This section is prose-only, explaining approach, not listing publications.

## 3. Data Analysis

### 3.1 [Heading matching actual topic and data]
Deep-dive quantitative findings (minimum 220 words). ALL numbers in markdown tables. Lead with 1-2 paragraphs of analysis, then a **MANDATORY bullet list** (3-5 items, not optional) calling out the standout data points or anomalies (e.g. "- IT sector returned 34% YTD, highest across all NIFTY sectors"). Explain what the data shows and why it matters. Insert [CHART_n] immediately after the paragraph whose data it visualises. Do NOT submit this subsection as pure paragraph prose — the bullet list must be present.

### 3.2 [Second dimension of analysis]
Comparisons, breakdowns, benchmarks (minimum 220 words). Use a mix of formats: 1-2 paragraphs of context + a **MANDATORY bullet list** of key differentiators or ranked highlights (not optional) + tables + [CHART_n]. List top/bottom performers or distinguishing characteristics as bullets, not buried in paragraph sentences.

### 3.3 [Trends or forward-looking data — only if sources have it]
Only include if sources have trend/time-series data with ≥4 distinct time points. Minimum 180 words if included. Use prose for the overall trend narrative; use a **MANDATORY bullet list** for discrete inflection points or catalysts (e.g. "- Q3 FY24: RBI rate pause triggered a rally in rate-sensitive sectors").

### 3.4 [A fourth distinct angle — peer/sector context, valuation, ownership, regulatory backdrop, etc. — only if sources genuinely support it]
Optional but encouraged when the sources have material left over after 3.1-3.3. Minimum 180 words if included. Skip entirely (don't stub it) if there's nothing left to say with real data.

## 4. Key Findings
8-12 findings. Each finding must follow this exact format — a **bold lead sentence** with a specific stat, then 1-2 sentences of plain explanation:
**1. [Bold stat-driven headline — e.g. "IT sector surged 34% YTD, outpacing all peers"]** — explanation of what it means and why it matters to investors.
No bracket citation markers — if attribution matters, name the publication in the sentence itself.

## 5. Risks & Considerations
3-5 distinct risks. Each risk uses a **bold label** followed by 2-3 sentences of explanation (minimum 200 words total). Format:
**[Risk Name — e.g. "Valuation Stretch"]:** explanation grounded in what the sources flag — regulatory risk, competitive pressure, valuation concerns, macro sensitivity, data gaps, etc. If sources don't surface explicit risks, reason from data patterns themselves. Do not skip this section.

## 6. Conclusion
2-3 paragraphs of synthesis (minimum 160 words) followed by a **"Key Takeaways"** bullet list (4-6 items, one per line) summarising the most important points for investors/stakeholders. End with a short forward-looking paragraph on outlook grounded in sources only.

## 7. Data Sources
One markdown table of sources (Publication | URL | Data type). One short intro sentence is fine, but do NOT repeat the methodology narrative here — that lives only in section 2. This section is the table itself, nothing more.
SOURCE BREADTH: list EVERY distinct publication below that contributed any real fact, figure, or context — not only whichever source happened to have the most granular numbers. If five sources were provided and three had usable content (numbers, context, definitions, market commentary), the table should show three rows, not one. Only cite a single source if every other source genuinely had nothing usable (e.g. paywalled, off-topic, or duplicate of another result) — and if so, do not claim more sources were used than actually were. Never list a publication that contributed nothing to the report.
URL COLUMN: the URL column must contain the EXACT "Source: <url>" value given for that publication in the supplementary sources below — never a description, a paraphrase, or any placeholder text standing in for a missing link. If a publication's URL truly is not in the source list, omit that row entirely from the table rather than writing anything else in its place.
This table is the ONLY place sources are listed. Do NOT add a separate "8. Sources" section, a second bulleted source list, or any other repeated listing of the same publications/URLs after this table — one table, once, is the complete Data Sources section. The report ends here, after this table.

GLOBAL RULES:
- FORMATTING DENSITY — MANDATORY: no section may run more than 2 consecutive paragraphs without
  a structural break — a bullet list, a table, or a chart. Sections 3.1 and 3.2 EACH require their
  own bullet list even if section 1 or 3 already had one; "the report has bullets somewhere" does
  not satisfy a specific section's requirement. If you catch yourself writing a 4th paragraph in a
  row with no bullets/table/chart between, stop and convert part of it into a bullet list instead —
  break out specific numbers, named entities, or ranked items as list items rather than narrating
  them inside a sentence.
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
- NEVER cite "Tavily" as a publication or source — Tavily is an internal search tool, not a publisher. If a fact's only origin is an internal search summary rather than a named publication, state the fact without attribution rather than inventing a citation.
- At least 4 markdown data tables
- 2-4 relevant images selected from the candidate list (0 acceptable only if none are genuinely relevant)
- keyStats: 6-8 real metrics with values and change indicators
- MINIMUM LENGTH — HARD REQUIREMENT: The "report" field MUST be at least 2800 words (≈18,000 characters).
  This is a strict floor enforced server-side — responses shorter than 15,000 characters will be REJECTED
  and regenerated. Every section listed above has its own minimum word count; meet them ALL.
  Write deeply: explain the mechanism behind every data point, give historical context, compare to peers,
  discuss implications for different investor types. Never stub any section. Length comes from depth and
  analysis, not repetition or invented facts.
  Target 2800-3500 words total across all sections.
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
1. Write sections in order: Introduction → Methodology → Data Analysis → Key Findings → Risks → Conclusion → Data Sources.
   Do NOT skip or abbreviate any section to save tokens.
2. Pace yourself: if you are past section 4 (Key Findings) and have written fewer than 10000 chars
   of report content, you are on track — keep going, do NOT start compressing.
3. The "charts" array must be COMPLETE before you close the JSON. If you run low on space, write
   shorter chart titles but include ALL chart objects.
4. Always end the JSON with: "summary": "...", "keyStats": [...]} — never leave it open.
5. If a section runs shorter than its minimum, EXPAND it with more analysis rather than moving on.
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
            if len(header_cells) >= 2 and len(rows) >= 2 and not is_metadata_table:
                # Defensive scrub: if a column is literally a URL/Link column,
                # blank any cell that isn't an actual http(s) link rather than
                # let placeholder/descriptive text (e.g. an LLM echoing a
                # "missing data" caveat) through to the rendered table.
                url_col_idxs = [
                    idx for idx, h in enumerate(header_cells)
                    if h.strip().lower() in ("url", "link", "source url")
                ]
                if url_col_idxs:
                    for row in rows:
                        for idx in url_col_idxs:
                            if idx < len(row) and not re.match(r"^https?://", row[idx].strip()):
                                row[idx] = "—"

                charts.append({
                    "type":    "table",
                    "title":   last_heading or "Data Table",
                    "columns": header_cells,
                    "rows":    rows,
                })
                out.append(f"[CHART_{len(charts)}]")
                i = j
                continue
            # Too small, or a Sources/Methodology-style metadata table —
            # leave it as plain markdown rather than charting it.

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
                if len(text) < 18_000:
                    log.warning(
                        "Groq: report too short (%d chars < 18000) — skipping, falling through to Gemini",
                        len(text),
                    )
                    continue
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
GEMINI_MODELS = [
    "gemini-2.5-flash",        # 65 536 output tokens — fast + high quality
    "gemini-3.5-flash",        # 65 536 output tokens — next-gen flash
    "gemini-3-flash-preview",  # 65 536 output tokens — Gemini 3 preview
    "gemini-2.5-flash-lite",   # 32 768 output tokens — last resort (smallest window)
]

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

    # Filter to AIzaSy* keys only — gen-lang-client-* are Vertex AI keys that
    # return 400 Bad Request on generativelanguage.googleapis.com REST API.
    rest_keys = [k for k in keys if k.startswith("AIzaSy")]
    if not rest_keys:
        log.warning("Gemini: no AIzaSy* keys available — all keys are gen-lang-client type")
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
        for key in _key_order(model):
            attempts.append((key, model))

    for key, model in attempts:
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
                    # Model doesn't exist — no point trying other keys for this model
                    log.warning("Gemini 404 — model=%s is deprecated/unavailable, skipping all keys for it", model)
                    break
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
                if len(text) < 18_000:
                    log.warning(
                        "Gemini: report too short (%d chars < 18000) — model=%s key=...%s — retrying next slot",
                        len(text), model, key[-4:],
                    )
                    continue
                return text, model
            log.warning("Gemini: empty response from model=%s key=...%s", model, key[-4:])
        except Exception as exc:
            log.warning("Gemini exception model=%s key=...%s: %s", model, key[-4:], exc)
            continue

    log.error("Gemini: all key×model combinations exhausted")
    return "", ""


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
    sense alongside the prior turn. Fold in the most recent assistant
    response (and the user question that produced it) so the search engine
    gets real grounding instead of a bare pronoun-heavy fragment.

    This deliberately makes the query longer than `question` alone — that's
    fine, tavily_search() truncates to Tavily's 400-char API limit on a word
    boundary before it ever hits the wire.
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

    return f"{question} — context: {last_assistant[:300]}"


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
            search_query = _build_followup_search_query(question, conversation_context)
            searched = await _tavily_search(search_query, max_results=20, min_results=10)
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
        search_query = _build_followup_search_query(question, conversation_context)
        if search_query != question:
            log.info("Report: search query enriched with prior context (%d → %d chars)",
                      len(question), len(search_query))
        searched = await _tavily_search(search_query, max_results=20)
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
    enriched = list(await asyncio.gather(*[enrich(s, i) for i, s in enumerate(sources[:20])]))
    log.info("Report: enrichment done (%d sources ready)", len(enriched))

    src_text = "\n\n---\n\n".join(
        f"- **{s['title']}**\nSource: {s['url']}\n"
        + (s["fullContent"][:1500] if len(s.get("fullContent", "")) > len(s.get("snippet", "")) else s.get("snippet", "")[:800])
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

    user_prompt = (
        f"Research Question / Topic (this — and ONLY this — defines the report's title and subject): {question}\n"
        + (f"\nPRIMARY SOURCE — ANALYSE THIS FIRST (uploaded file data takes highest priority):{file_section}" if file_section else "")
        + conversation_section
        + (img_placement_instruction if img_placement_instruction else "")
        + f"\n\nSupplementary web sources ({len(enriched)} results):\n\n{src_text}\n\n"
        + image_candidates_block
        + "INSTRUCTIONS:\n"
        "1. The uploaded file content (if provided) is your PRIMARY source — extract ALL numbers, tables, charts, and statistics from it first.\n"
        "2. Use web sources to supplement and validate the file data.\n"
        "3. Follow CHART RULES exactly — reproduce actual data from the file as charts where it exists.\n"
        "4. Write the full 6-section, long-form report (target 2800-3500 words (MINIMUM 18000 characters — shorter responses will be rejected and retried)). Insert [CHART_n] inline where valid chart data exists.\n"
        "5. Do NOT insert any [WEB_IMG_n] markers and do not return an \"images\" array — this report uses charts/graphs/tables only, never stock or decorative photos.\n"
        + ("6. Insert [FILE_IMG_n] references inline where you reference data visible in that extracted image/chart.\n" if embedded_file_images else "")
        + "7. Respond ONLY with the JSON object — no markdown fences, no text outside JSON."
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
            if n_labels < min_labels:
                log.warning("Chart rejected — only %d distinct items (need ≥%d for %s chart): %s",
                            n_labels, min_labels, chart_type, ch.get("title", "?"))
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

    try:
        parsed = json.loads(clean)

        original_charts_list = parsed.get("charts") or []
        valid_mask = [_is_plausible_chart(c) for c in original_charts_list]
        charts = [c for c, keep in zip(original_charts_list, valid_mask) if keep]
        if len(charts) < len(original_charts_list):
            log.info("Chart validation: kept %d / %d charts", len(charts), len(original_charts_list))

        report_text = parsed.get("report", "")
        # Renumber/strip [CHART_n] placeholders so they still point at the
        # right chart now that some may have been dropped above — otherwise
        # every placeholder after a rejected chart points one slot too far
        # into the now-shorter array (see _remap_chart_placeholders docstring).
        report_text = _remap_chart_placeholders(report_text, original_charts_list, valid_mask)

        original_images_list = parsed.get("images") or []
        images, images_valid_mask = _validate_image_selections(original_images_list, image_candidates)
        if len(images) < len(original_images_list):
            log.info("Image validation: kept %d / %d selections", len(images), len(original_images_list))

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
                if len(report_raw) > 200:
                    result["report"] = report_raw
            arr_text = _extract_balanced_array(text, "keyStats")
            if arr_text:
                try: result["keyStats"] = json.loads(arr_text)
                except Exception: pass
            arr_text = _extract_balanced_array(text, "charts")
            if arr_text:
                try:
                    result["charts"] = json.loads(arr_text)
                except Exception: pass
            # Also try to salvage images array from truncated JSON
            arr_text = _extract_balanced_array(text, "images")
            if arr_text:
                try:
                    raw_imgs = json.loads(arr_text)
                    if image_candidates and raw_imgs:
                        validated, _ = _validate_image_selections(raw_imgs, image_candidates)
                        result["images"] = validated
                except Exception:
                    pass
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
            _raw_charts = salvaged.get("charts") or []
            _valid_mask = [_is_plausible_chart(c) for c in _raw_charts]
            _salv_charts = [c for c, keep in zip(_raw_charts, _valid_mask) if keep]
            if len(_salv_charts) < len(_raw_charts):
                log.info("Chart validation (salvage): kept %d / %d charts", len(_salv_charts), len(_raw_charts))
            salvaged["report"] = _remap_chart_placeholders(salvaged.get("report", ""), _raw_charts, _valid_mask)
            salvaged["report"], _salv_charts = _extract_inline_chart_jsons(salvaged["report"], _salv_charts)
            salvaged["report"], _salv_charts = _extract_markdown_tables(salvaged["report"], _salv_charts)
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
            repaired_imgs = []
            try:
                raw_imgs = repaired_parsed.get("images") or []
                if image_candidates and raw_imgs:
                    repaired_imgs, _ = _validate_image_selections(raw_imgs, image_candidates)
            except Exception:
                pass
            repaired_imgs, _ = _force_fallback_images(repaired_imgs, image_candidates, model_used)
            repaired_imgs, _ = _top_up_images(repaired_imgs, image_candidates, model_used)
            _raw_charts = repaired_parsed.get("charts") or []
            _valid_mask = [_is_plausible_chart(c) for c in _raw_charts]
            _rep_charts = [c for c, keep in zip(_raw_charts, _valid_mask) if keep]
            if len(_rep_charts) < len(_raw_charts):
                log.info("Chart validation (repair): kept %d / %d charts", len(_rep_charts), len(_raw_charts))
            repaired_report = _remap_chart_placeholders(repaired_report, _raw_charts, _valid_mask)
            repaired_report, _rep_charts = _extract_inline_chart_jsons(repaired_report, _rep_charts)
            repaired_report, _rep_charts = _extract_markdown_tables(repaired_report, _rep_charts)
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
        except Exception:
            pass

        log.error("Report: could not salvage JSON — returning error message")
        return JSONResponse({
            "title": _sanitize_title("", question),
            "report": "## Report Generation Error\n\nThe AI response could not be parsed. Please try a more specific question.",
            "charts": [], "images": [], "keyStats": [], "summary": "", "fileImages": embedded_file_images,
        })

