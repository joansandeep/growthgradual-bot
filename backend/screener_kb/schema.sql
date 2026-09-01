-- Screener.in fundamentals knowledge base — Supabase/Postgres schema
-- Run this once in the Supabase SQL editor (or via psql) before running the loader.
create extension if not exists vector;
create table if not exists companies (
    id              bigint generated always as identity primary key,
    ticker          text unique not null,
    name            text not null,
    company_url     text,
    website         text,
    consolidated    boolean,
    current_price   numeric,
    price_change_pct numeric,
    price_date_label text,        -- Screener's raw label, e.g. "28 Aug - close price"
    market_cap      numeric,      -- denormalized from Top_Ratios for fast filtering
    pe_ratio        numeric,      -- denormalized from Top_Ratios
    raw_info        jsonb,        -- full Company_Info row, nothing lost
    source_file     text,
    updated_at      timestamptz default now()
);
create table if not exists ratios (                -- Top_Ratios: point-in-time snapshot
    company_id  bigint references companies(id) on delete cascade,
    metric      text,
    raw_value   text,
    value       numeric,
    as_of       date default current_date,
    primary key (company_id, metric, as_of)
);
create table if not exists financials (             -- P&L / Balance Sheet / Cash Flow / Ratios(history) / Quarterly, melted long
    company_id  bigint references companies(id) on delete cascade,
    statement   text,            -- 'Quarterly' | 'Annual P&L' | 'Balance Sheet' | 'Cash Flow' | 'Annual Ratios'
    period      text,            -- column header from the sheet, e.g. '2024-03-31' or '2025-09-30'
    line_item   text,            -- e.g. 'Sales', 'Net Profit', 'Total Assets' (trailing screener '+' stripped)
    raw_value   text,
    value       numeric,
    primary key (company_id, statement, period, line_item)
);
create table if not exists growth_cagr (
    company_id  bigint references companies(id) on delete cascade,
    metric      text,            -- 'Compounded Sales Growth', 'Return on Equity', etc.
    period      text,            -- '10 Years', '5 Years', '3 Years', 'TTM' / 'Last Year'
    raw_value   text,
    value       numeric,
    primary key (company_id, metric, period)
);
create table if not exists shareholding (
    company_id  bigint references companies(id) on delete cascade,
    frequency   text,            -- 'Quarterly' | 'Yearly'
    holder_type text,            -- 'Promoters', 'FIIs', 'DIIs', 'Public', 'No. of Shareholders'
    period      text,            -- column header, e.g. 'Jun 2026'
    raw_value   text,
    value       numeric,
    primary key (company_id, frequency, holder_type, period)
);
create table if not exists peers (
    company_id  bigint references companies(id) on delete cascade,
    peer_name   text,
    metrics     jsonb,           -- full peer row (CMP, P/E, Mar Cap, ROCE, etc.) — columns vary, keep flexible
    row_num     int,
    primary key (company_id, row_num)
);
create table if not exists pros_cons (
    company_id  bigint references companies(id) on delete cascade,
    kind        text check (kind in ('Pros','Cons')),
    point       text,
    row_num     int,
    primary key (company_id, row_num)
);
create table if not exists documents (
    company_id  bigint references companies(id) on delete cascade,
    title       text,
    url         text,
    row_num     int,
    primary key (company_id, row_num)
);
-- Semantic layer for the chatbot / RAG
create table if not exists company_summaries (
    company_id  bigint references companies(id) on delete cascade primary key,
    summary     text,
    embedding   vector(1536)     -- match the dimension of whatever model rag_engine._embed() uses
);
create index if not exists idx_financials_company on financials(company_id);
create index if not exists idx_ratios_company on ratios(company_id);
create index if not exists idx_summaries_embedding on company_summaries
    using ivfflat (embedding vector_cosine_ops);
alter table companies         enable row level security;
alter table ratios            enable row level security;
alter table financials        enable row level security;
alter table growth_cagr       enable row level security;
alter table shareholding      enable row level security;
alter table peers             enable row level security;
alter table pros_cons         enable row level security;
alter table documents         enable row level security;
alter table company_summaries enable row level security;

create policy "public read" on companies         for select using (true);
create policy "public read" on ratios            for select using (true);
create policy "public read" on financials        for select using (true);
create policy "public read" on growth_cagr       for select using (true);
create policy "public read" on shareholding      for select using (true);
create policy "public read" on peers             for select using (true);
create policy "public read" on pros_cons         for select using (true);
create policy "public read" on documents         for select using (true);
create policy "public read" on company_summaries for select using (true);
