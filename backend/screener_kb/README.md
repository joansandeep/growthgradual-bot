# Screener.in Fundamentals Knowledge Base

Structured company-fundamentals data (ratios, quarterly/annual financials,
growth CAGR, shareholding pattern, peers, pros/cons, filing links) loaded
from Screener.in export workbooks into Postgres/Supabase, and wired into
Growth Gradual's chat + API layer.

## One-time setup

1. Run `schema.sql` once in the Supabase SQL editor (same project as the
   rest of the app, unless you set `SCREENER_SUPABASE_URL` /
   `SCREENER_SUPABASE_ANON_KEY` to point elsewhere).
2. `pip install psycopg2-binary pandas openpyxl`
3. `export DATABASE_URL="postgresql://postgres:PASSWORD@HOST:5432/postgres"`
   (Supabase dashboard → Settings → Database → Connection string → URI —
   the direct Postgres string, not the REST/anon key.)
4. `python load_screener_data.py /path/to/companies_folder`

Re-running is safe — companies upsert on `ticker`, every other table
upserts on its own primary key.

## How the backend uses it

- `backend/utils/screener_kb.py` — reads the same tables at runtime via
  Supabase's PostgREST API (reusing `SUPABASE_URL` / `SUPABASE_ANON_KEY`,
  the same pattern already used in `utils/keys.py` and `routes/chat.py`).
  It keeps a lightweight in-memory `{ticker, name}` index (refreshed every
  6h) to resolve a company mentioned in free-text chat without an LLM call,
  then fetches and formats that company's full snapshot into a compact
  markdown context block.
- `backend/routes/chat.py` — every non-smalltalk chat turn now runs a KB
  lookup in parallel with the existing web search / headlines fetch. If a
  company is confidently resolved, its fundamentals block is injected into
  the system prompt as an authoritative `COMPANY FUNDAMENTALS DATABASE`
  section (ranked above web search snippets for that company's numbers),
  and `kbMatched` is surfaced in the `meta` SSE event.
- `backend/routes/report.py` — the research-engine's stock/valuation data-gathering step (triggered by `_STOCK_COMPANY_INTENT_RE`, the same gate used for the existing Yahoo live-fundamentals fetch) now also calls `fetch_screener_fundamentals()` for the same extracted company candidates and prepends a `format_screener_snapshots_as_source()` block ahead of everything else in `sources`. Unlike the Yahoo snapshot (today's price/valuation only), this block lays out each quarterly/annual financial line item, growth CAGR, and shareholding metric as an explicit period-by-period series (oldest→latest) — built specifically so the report-writing model can lift real multi-point series straight into charts/tables instead of only ever having a single data point per company.
- `backend/routes/stocks.py` — new REST endpoints for direct/frontend use:
  - `GET /api/stocks/search?q=reliance` — ticker/name search
  - `GET /api/stocks/{company_id}` — full snapshot (ratios, financials,
    growth, shareholding, peers, pros/cons) plus the same formatted context
    block used in chat.

No new environment variables are required if the knowledge base lives in
the same Supabase project as sessions/Paperly. To use a separate project,
set `SCREENER_SUPABASE_URL` and `SCREENER_SUPABASE_ANON_KEY`.

## Company resolution heuristic

`_resolve_from_index()` in `screener_kb.py` matches, in order of
confidence: (1) an exact ticker word in the message, (2) the full
normalized company name appearing verbatim, (3) a single distinctive
(non-generic, 4+ character) name token shared with the message. It
deliberately returns no match rather than guessing when a query is
ambiguous or matches multiple companies — a missed lookup just falls back
to the existing web-search flow, whereas a wrong match would poison the
answer with another company's financials.
