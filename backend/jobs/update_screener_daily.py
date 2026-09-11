"""
Daily Screener.in -> Supabase updater for Growth Gradual.

Production mode is DB-backed: the existing `companies` table is the persistent
stock universe, so this job does not depend on a local workbook directory or a
Render persistent disk. It refreshes each company snapshot into an ephemeral
work directory, then upserts it into the Screener KB.

Modes:
  python jobs/update_screener_daily.py --mode daily --from-db
  python jobs/update_screener_daily.py --mode daily --from-db --limit 5
  python jobs/update_screener_daily.py --mode full --from-db

Render runs the lightweight daily mode at 12:00 IST (06:30 UTC).
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import psycopg2

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("gg_screener_daily")

JOB_DIR = Path(__file__).resolve().parent
SCRAPER_PATH = JOB_DIR.parent / "screener_kb" / "screener_all_stock_data.py"
LOADER_PATH = JOB_DIR.parent / "screener_kb" / "load_screener_data.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required for the daily stock updater")
    return value


def get_company_urls(conn) -> list[str]:
    """Read the persistent company universe from Supabase/Postgres."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select company_url
            from companies
            where company_url is not null and trim(company_url) <> ''
            order by ticker
            """
        )
        return [row[0] for row in cur.fetchall() if row[0]]


def ensure_run_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            create table if not exists stock_data_update_runs (
                run_id bigserial primary key,
                started_at timestamptz not null default now(),
                finished_at timestamptz,
                mode text not null,
                selected_count integer not null default 0,
                succeeded_count integer not null default 0,
                failed_count integer not null default 0,
                status text not null default 'running',
                notes text
            )
            """
        )
    conn.commit()


def start_run(conn, mode: str, selected_count: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into stock_data_update_runs (mode, selected_count, status)
            values (%s, %s, 'running')
            returning run_id
            """,
            (mode, selected_count),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return int(run_id)


def finish_run(conn, run_id: int, succeeded: int, failed: int, status: str, notes: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update stock_data_update_runs
            set finished_at = now(), succeeded_count = %s, failed_count = %s,
                status = %s, notes = %s
            where run_id = %s
            """,
            (succeeded, failed, status, notes[:4000], run_id),
        )
    conn.commit()


def scrape_one(scraper, url: str, mode: str):
    try:
        info, sheets, output_file = scraper.scrape_company(url)
        ticker = scraper.clean_text(info.get("Ticker", ""))
        if not ticker:
            raise RuntimeError("Company page returned no ticker")
        records = scraper.flatten_company(info, sheets)
        return {
            "ok": True,
            "url": url,
            "ticker": ticker,
            "name": scraper.clean_text(info.get("Company Name", "")),
            "output_file": str(output_file),
            "records": len(records),
        }
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def load_workbook(loader, conn, path: Path) -> None:
    with conn.cursor() as cur:
        loader.load_workbook(cur, path)
    conn.commit()


def run(mode: str, limit: int | None = None) -> int:
    started = time.time()
    db_url = get_database_url()
    conn = psycopg2.connect(db_url)
    scraper = None
    loader = None
    run_id = None
    temp_root = Path(tempfile.mkdtemp(prefix="gg-screener-daily-"))
    try:
        ensure_run_table(conn)
        urls = get_company_urls(conn)
        if limit and limit > 0:
            urls = urls[:limit]
        if not urls:
            raise RuntimeError("No company URLs found in companies table")

        run_id = start_run(conn, mode, len(urls))
        log.info("Screener daily run %s | mode=%s | companies=%s | date=%s", run_id, mode, len(urls), date.today())

        scraper = load_module(SCRAPER_PATH, "gg_screener_scraper")
        loader = load_module(LOADER_PATH, "gg_screener_loader")

        # The original collector writes XLSX workbooks. Keep that intermediate
        # representation for compatibility with the proven loader, but put it on
        # ephemeral job storage instead of relying on a persistent disk.
        scraper.OUTPUT_DIR = temp_root
        scraper.COMPANY_DIR = temp_root / "companies"
        scraper.RAW_DIR = temp_root / "raw_html"
        scraper.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        scraper.COMPANY_DIR.mkdir(parents=True, exist_ok=True)
        scraper.SAVE_RAW_HTML = False

        if mode == "daily":
            # Daily refresh should update the structured fundamentals without the
            # expensive full historical chart fan-out. Existing historical
            # financial periods remain in Postgres, and new periods are upserted.
            scraper.REQUIRE_CHARTS = False
            scraper.extract_chart_data = lambda soup, info, company_url: {}
        else:
            scraper.REQUIRE_CHARTS = True

        workers = max(1, int(os.environ.get("SCREENER_UPDATE_WORKERS", getattr(scraper, "WORKERS", 2))))
        workers = min(workers, 4)
        log.info("Worker count: %s", workers)

        succeeded = 0
        failed = []
        refreshed = []

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gg-screener") as pool:
            futures = {pool.submit(scrape_one, scraper, url, mode): url for url in urls}
            for index, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                if not result["ok"]:
                    failed.append(result)
                    log.warning("[%s/%s] FAILED %s | %s", index, len(urls), result["url"], result["error"])
                    continue

                succeeded += 1
                refreshed.append(Path(result["output_file"]))
                if index % 25 == 0 or index == len(urls):
                    log.info("Progress %s/%s | succeeded=%s failed=%s", index, len(urls), succeeded, len(failed))

        # Load each refreshed workbook into Postgres. Historical rows are protected
        # by primary keys; newly published periods/snapshots are added.
        loaded = 0
        for path in refreshed:
            try:
                load_workbook(loader, conn, path)
                loaded += 1
            except Exception as exc:
                failed.append({"url": str(path), "error": f"DB load failed: {exc}"})
                conn.rollback()
                log.exception("DB load failed: %s", path)

        status = "success" if not failed else "partial"
        notes = f"loaded={loaded}; mode={mode}; elapsed={time.time() - started:.1f}s"
        finish_run(conn, run_id, succeeded, len(failed), status, notes)
        log.info("Completed run %s | status=%s | succeeded=%s failed=%s loaded=%s | %.1fs", run_id, status, succeeded, len(failed), loaded, time.time() - started)
        return 0 if not failed else 2
    except Exception as exc:
        if run_id is not None:
            try:
                finish_run(conn, run_id, 0, 1, "failed", str(exc))
            except Exception:
                conn.rollback()
        log.exception("Daily stock update failed: %s", exc)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass
        shutil.rmtree(temp_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("daily", "full"), default="daily")
    parser.add_argument("--from-db", action="store_true", help="Use the Supabase companies table as the persistent stock universe")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if not args.from_db:
        parser.error("--from-db is required; production runs use the persistent DB company universe")
    return run(args.mode, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
