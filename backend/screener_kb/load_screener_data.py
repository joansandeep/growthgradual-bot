"""
Load screener.in company workbooks (produced by screener_all_stock_data.py)
into the Postgres/Supabase schema defined in schema.sql.

Setup
-----
1. In the Supabase SQL editor, run schema.sql once.
2. pip install psycopg2-binary pandas openpyxl
3. Set DATABASE_URL to your Supabase Postgres connection string
   (Supabase dashboard -> Settings -> Database -> Connection string -> URI,
   NOT the REST/anon key -- this script talks to Postgres directly).

Usage
-----
    export DATABASE_URL="postgresql://postgres:PASSWORD@HOST:5432/postgres"
    python load_screener_data.py /path/to/companies_folder

Re-running is safe: companies are upserted on ticker, and every other table
uses ON CONFLICT DO UPDATE on its primary key, so re-loading a workbook just
refreshes its rows.
"""
import os
import re
import sys
import glob
import logging

import pandas as pd
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loader")

BATCH_SIZE = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_numeric(val):
    """Best-effort parse of screener's mixed number formats into a float."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val == val else None  # filter NaN
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "-", "na"):
        return None
    s = s.replace("₹", "").replace(",", "").replace("Cr.", "").strip()
    s = s.replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def clean_line_item(label):
    """Strip screener's trailing ' +' (expandable row marker) and whitespace."""
    if label is None:
        return None
    return re.sub(r"\s*\+\s*$", "", str(label)).strip()


def safe_str(val):
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s.lower() != "nan" else None


# ---------------------------------------------------------------------------
# Per-sheet parsers -> list[dict] rows ready for insertion
# ---------------------------------------------------------------------------

def parse_company_info(df):
    if df.empty:
        return None
    row = df.iloc[0]
    return {
        "ticker": safe_str(row.get("Ticker")),
        "name": safe_str(row.get("Company Name")),
        "company_url": safe_str(row.get("Company URL")),
        "website": safe_str(row.get("Website")),
        "consolidated": bool(row.get("Consolidated")) if row.get("Consolidated") is not None else None,
        "current_price": to_numeric(row.get("Current Price")),
        "price_change_pct": to_numeric(row.get("Price Change %")),
        "price_date_label": safe_str(row.get("Price Date")),
        "raw_info": row.dropna().to_dict(),
    }


def parse_top_ratios(df):
    out = []
    for _, r in df.iterrows():
        out.append({
            "metric": safe_str(r.get("Metric")),
            "raw_value": safe_str(r.get("Value")),
            "value": to_numeric(r.get("Numeric Value", r.get("Value"))),
        })
    return out


def melt_wide_statement(df, statement_name):
    """Sheets shaped like: first column = line item label, remaining columns = periods."""
    if df.empty or df.shape[1] < 2:
        return []
    id_col = df.columns[0]
    period_cols = [c for c in df.columns[1:]]
    rows = []
    for _, r in df.iterrows():
        line_item = clean_line_item(r.get(id_col))
        if not line_item:
            continue
        for pc in period_cols:
            raw = r.get(pc)
            if safe_str(raw) is None:
                continue
            rows.append({
                "statement": statement_name,
                "period": str(pc),
                "line_item": line_item,
                "raw_value": safe_str(raw),
                "value": to_numeric(raw),
            })
    return rows


def parse_growth_cagr(df):
    out = []
    for _, r in df.iterrows():
        period = safe_str(r.get("Period"))
        if period:
            period = period.rstrip(":").strip()
        out.append({
            "metric": safe_str(r.get("Metric")),
            "period": period,
            "raw_value": safe_str(r.get("Value")),
            "value": to_numeric(r.get("Numeric Value", r.get("Value"))),
        })
    return out


def parse_shareholding(df, frequency):
    if df.empty or df.shape[1] < 2:
        return []
    id_col = df.columns[0]
    period_cols = df.columns[1:]
    out = []
    for _, r in df.iterrows():
        holder = clean_line_item(r.get(id_col))
        if not holder:
            continue
        for pc in period_cols:
            raw = r.get(pc)
            if safe_str(raw) is None:
                continue
            out.append({
                "frequency": frequency,
                "holder_type": holder,
                "period": str(pc),
                "raw_value": safe_str(raw),
                "value": to_numeric(raw),
            })
    return out


def parse_peers(df):
    out = []
    for i, r in df.iterrows():
        d = r.dropna().to_dict()
        if not d:
            continue
        out.append({
            "peer_name": safe_str(r.get("Name")),
            "metrics": {k: (v if not isinstance(v, float) else v) for k, v in d.items()},
            "row_num": i,
        })
    return out


def parse_pros_cons(df):
    out = []
    for i, r in df.iterrows():
        kind = safe_str(r.get("Type"))
        point = safe_str(r.get("Point"))
        if not kind or not point:
            continue
        out.append({"kind": kind, "point": point, "row_num": i})
    return out


def parse_documents(df):
    out = []
    for i, r in df.iterrows():
        title = safe_str(r.get("Document"))
        url = safe_str(r.get("URL"))
        if not url:
            continue
        out.append({"title": title, "url": url, "row_num": i})
    return out


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def upsert_company(cur, info, source_file):
    if not info or not info.get("ticker"):
        return None
    ratios_lookup = info.pop("_ratios_lookup", {})
    cur.execute(
        """
        insert into companies (ticker, name, company_url, website, consolidated,
                                current_price, price_change_pct, price_date_label,
                                market_cap, pe_ratio, raw_info, source_file, updated_at)
        values (%(ticker)s, %(name)s, %(company_url)s, %(website)s, %(consolidated)s,
                %(current_price)s, %(price_change_pct)s, %(price_date_label)s,
                %(market_cap)s, %(pe_ratio)s, %(raw_info)s, %(source_file)s, now())
        on conflict (ticker) do update set
            name = excluded.name,
            company_url = excluded.company_url,
            website = excluded.website,
            consolidated = excluded.consolidated,
            current_price = excluded.current_price,
            price_change_pct = excluded.price_change_pct,
            price_date_label = excluded.price_date_label,
            market_cap = excluded.market_cap,
            pe_ratio = excluded.pe_ratio,
            raw_info = excluded.raw_info,
            source_file = excluded.source_file,
            updated_at = now()
        returning id
        """,
        {
            **info,
            "market_cap": ratios_lookup.get("Market Cap"),
            "pe_ratio": ratios_lookup.get("Stock P/E"),
            "raw_info": psycopg2.extras.Json(info["raw_info"]),
            "source_file": source_file,
        },
    )
    return cur.fetchone()[0]


def bulk_upsert(cur, table, columns, rows, company_id, conflict_cols):
    if not rows:
        return
    cols = ["company_id"] + columns
    values = [tuple([company_id] + [row.get(c) for c in columns]) for row in rows]
    update_cols = [c for c in columns if c not in conflict_cols]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols) or "company_id = excluded.company_id"
    sql = (
        f"insert into {table} ({', '.join(cols)}) values %s "
        f"on conflict ({', '.join(['company_id'] + conflict_cols)}) do update set {set_clause}"
    )
    psycopg2.extras.execute_values(cur, sql, values, page_size=BATCH_SIZE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SHEET_MAP = {
    "Quarterly Results": "Quarterly",
    "Profit & Loss_1": "Annual P&L",
    "Balance Sheet": "Balance Sheet",
    "Cash Flow": "Cash Flow",
    "Ratios": "Annual Ratios",
}


def load_workbook(cur, path):
    xl = pd.ExcelFile(path)
    sheets = {name: xl.parse(name) for name in xl.sheet_names}

    info = parse_company_info(sheets.get("Company_Info", pd.DataFrame()))
    if not info:
        log.warning("Skipping %s: no Company_Info sheet", path)
        return

    ratios_rows = parse_top_ratios(sheets.get("Top_Ratios", pd.DataFrame())) if "Top_Ratios" in sheets else []
    info["_ratios_lookup"] = {r["metric"]: r["value"] for r in ratios_rows if r["metric"]}

    company_id = upsert_company(cur, info, os.path.basename(path))
    if company_id is None:
        log.warning("Skipping %s: could not resolve ticker", path)
        return

    bulk_upsert(cur, "ratios", ["metric", "raw_value", "value"], ratios_rows, company_id, ["metric", "as_of"])

    financials_rows = []
    for sheet_name, statement in SHEET_MAP.items():
        if sheet_name in sheets:
            financials_rows.extend(melt_wide_statement(sheets[sheet_name], statement))
    bulk_upsert(cur, "financials", ["statement", "period", "line_item", "raw_value", "value"],
                financials_rows, company_id, ["statement", "period", "line_item"])

    if "Growth_CAGR" in sheets:
        bulk_upsert(cur, "growth_cagr", ["metric", "period", "raw_value", "value"],
                    parse_growth_cagr(sheets["Growth_CAGR"]), company_id, ["metric", "period"])

    share_rows = []
    if "Shareholding_Quarterly" in sheets:
        share_rows.extend(parse_shareholding(sheets["Shareholding_Quarterly"], "Quarterly"))
    if "Shareholding_Yearly" in sheets:
        share_rows.extend(parse_shareholding(sheets["Shareholding_Yearly"], "Yearly"))
    bulk_upsert(cur, "shareholding", ["frequency", "holder_type", "period", "raw_value", "value"],
                share_rows, company_id, ["frequency", "holder_type", "period"])

    if "Peers" in sheets:
        rows = parse_peers(sheets["Peers"])
        bulk_upsert(cur, "peers", ["peer_name", "metrics", "row_num"],
                    [{**r, "metrics": psycopg2.extras.Json(r["metrics"])} for r in rows],
                    company_id, ["row_num"])

    if "Pros_Cons" in sheets:
        bulk_upsert(cur, "pros_cons", ["kind", "point", "row_num"],
                    parse_pros_cons(sheets["Pros_Cons"]), company_id, ["row_num"])

    if "Documents" in sheets:
        bulk_upsert(cur, "documents", ["title", "url", "row_num"],
                    parse_documents(sheets["Documents"]), company_id, ["row_num"])

    log.info("Loaded %s (company_id=%s)", info.get("name") or path, company_id)


def main():
    if len(sys.argv) != 2:
        print("Usage: python load_screener_data.py /path/to/companies_folder")
        sys.exit(1)

    folder = sys.argv[1]
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("Set DATABASE_URL to your Supabase Postgres connection string first.")
        sys.exit(1)

    files = sorted(glob.glob(os.path.join(folder, "*.xlsx")))
    if not files:
        print(f"No .xlsx files found in {folder}")
        sys.exit(1)

    log.info("Found %d workbooks", len(files))
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            for i, path in enumerate(files, 1):
                try:
                    load_workbook(cur, path)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    log.exception("Failed on %s -- rolled back, continuing", path)
                if i % 100 == 0:
                    log.info("Progress: %d/%d", i, len(files))
    finally:
        conn.close()

    log.info("Done.")


if __name__ == "__main__":
    main()
