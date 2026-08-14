-- Run once in the Supabase SQL editor.
--
-- Fixes: POST/PATCH https://<project>.supabase.co/rest/v1/sessions returning
-- 400 Bad Request for every authenticated chat request. routes/chat.py's
-- _upsert_session() attaches a "user_id" field to the session row whenever
-- the caller is logged in (so a session can later be tied back to an
-- account), but the "sessions" table was never given that column — PostgREST
-- rejects unknown JSON keys with PGRST204 ("Could not find the 'user_id'
-- column of 'sessions' in the schema cache"), which surfaces as a plain 400.
--
-- Safe to run even if the column already exists (IF NOT EXISTS).

alter table sessions add column if not exists user_id uuid references auth.users(id);

create index if not exists idx_sessions_user_id on sessions(user_id);
