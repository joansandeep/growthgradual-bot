const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false,
  max: 5, idleTimeoutMillis: 30000, connectionTimeoutMillis: 8000,
});

const connectDB = async () => {
  const client = await pool.connect();
  const { rows } = await client.query('SELECT NOW()');
  client.release();
  console.log(`✅ PostgreSQL connected: ${rows[0].now}`);
};

const ensureSchema = async () => {
  try {
    await pool.query(`
      CREATE TABLE IF NOT EXISTS sessions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        last_active TIMESTAMPTZ DEFAULT NOW(),
        query_count INT DEFAULT 0,
        user_id UUID
      );
      -- Table may already exist from before user_id was added (CREATE TABLE
      -- IF NOT EXISTS above is a no-op in that case) — backfill it here so
      -- routes/chat.py's PostgREST upsert (which sends a "user_id" key
      -- whenever the request is authenticated) doesn't 400 with
      -- "Could not find the 'user_id' column of 'sessions' in the schema
      -- cache" on every logged-in chat call. Left unconstrained (no FK to
      -- auth.users) so this also works on non-Supabase Postgres.
      ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id UUID;
      CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
      CREATE TABLE IF NOT EXISTS files (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        original_name TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        file_type TEXT NOT NULL,
        size_bytes BIGINT NOT NULL,
        extracted_text TEXT DEFAULT '',
        has_text BOOLEAN DEFAULT FALSE,
        ocr_processed BOOLEAN DEFAULT FALSE,
        word_count INT DEFAULT 0,
        page_count INT,
        processing_error TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );
      CREATE TABLE IF NOT EXISTS messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user','assistant')),
        content TEXT NOT NULL,
        llm_provider TEXT,
        tokens_used INT DEFAULT 0,
        response_ms INT DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );
      CREATE INDEX IF NOT EXISTS idx_files_session    ON files(session_id);
      CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
    `);
    console.log('✅ Schema ready');
  } catch (e) {
    // Tables may already exist with RLS — log but don't crash
    console.warn('⚠️  ensureSchema warning (may be RLS/already exists):', e.message);
  }
};

module.exports = { pool, connectDB, ensureSchema };
