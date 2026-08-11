-- Run once in the Supabase SQL editor.
-- Backs utils/keys.py's cross-restart persistence for long (24h, 403) API-key
-- bans, so they survive redeploys instead of resetting every time you push.
--
-- Only key HASHES are stored, never raw keys.

create table if not exists gg_key_bans (
    key_hash      text primary key,
    banned_until  timestamptz not null,
    updated_at    timestamptz not null default now()
);

-- Row Level Security: match the pattern already used by your other tables
-- (sessions/messages) — allow the anon key used by the backend to read/write.
-- If you'd rather keep RLS strict, use a service-role key for this table
-- instead and set SUPABASE_SERVICE_KEY in the backend env, or just leave
-- this table's writes failing silently — the code degrades gracefully to
-- in-memory-only bans if it can't reach this table (see keys.py docstring).
alter table gg_key_bans enable row level security;

create policy "gg_key_bans anon read" on gg_key_bans
    for select using (true);

create policy "gg_key_bans anon upsert" on gg_key_bans
    for insert with check (true);

create policy "gg_key_bans anon update" on gg_key_bans
    for update using (true);

-- Optional housekeeping: periodically clear expired rows so the table
-- doesn't grow forever. Safe to run manually or on a cron; not required
-- for correctness since load_persisted_bans() already filters by
-- banned_until > now().
-- delete from gg_key_bans where banned_until < now() - interval '7 days';
