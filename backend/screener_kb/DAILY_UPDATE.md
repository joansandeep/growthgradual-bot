# Growth Gradual daily Screener update

The Growth Gradual deployment now contains the Screener stock refresh job.

## Production (Render)

The root `render.yaml` declares a separate Render Cron Job named
`growth-gradual-screener-daily`. It runs at **06:30 UTC every day, which is
12:00 IST**, and executes:

```bash
python jobs/update_screener_daily.py --mode daily --from-db
```

Render cron jobs do not have persistent disks, so the job deliberately uses the
Supabase/Postgres `companies` table as the persistent stock universe and uses an
ephemeral working directory only for the XLSX hand-off between scraper and DB
loader.

Set `DATABASE_URL` on the cron service to the same Postgres/Supabase connection
string used by the Screener knowledge base.

## One-time catch-up

Before relying on the scheduled job, run the same command manually once. It
will refresh the existing company universe from its current database state,
which brings the stock snapshot forward to the latest Screener data available
at the time of the run.

For a 5-company smoke test:

```bash
python jobs/update_screener_daily.py --mode daily --from-db --limit 5
```

## What daily mode updates

- current price and price-date snapshot
- top ratios / valuation metrics
- newly available quarterly and annual financial periods
- growth/CAGR rows
- shareholding snapshots
- company documents/filing links
- the structured Screener knowledge base used by chat and research reports

Daily mode intentionally skips the expensive chart-API fan-out. Existing
financial history remains in Postgres, while newly published financial periods
are added/upserted.

## Audit trail

Each run writes to `stock_data_update_runs` with start/finish time, mode,
selected companies, successes, failures, and a short status note.
