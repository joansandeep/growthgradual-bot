require('dotenv').config();
const express = require('express');
const { connectDB, ensureSchema, pool } = require('./config/db');

const app  = express();
const PORT = process.env.PORT || process.env.SESSION_PORT || 4001;
app.use(express.json());

connectDB().then(ensureSchema).catch(console.error);

// Create session
app.post('/sessions', async (req, res) => {
  try {
    const { rows } = await pool.query(
      'INSERT INTO sessions DEFAULT VALUES RETURNING id, created_at'
    );
    res.json({ sessionId: rows[0].id, createdAt: rows[0].created_at });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// Get session
app.get('/sessions/:id', async (req, res) => {
  try {
    const { rows } = await pool.query(
      `SELECT s.id, s.created_at, s.last_active, s.query_count,
              COUNT(f.id)::int AS file_count
       FROM sessions s LEFT JOIN files f ON f.session_id = s.id
       WHERE s.id=$1 GROUP BY s.id`, [req.params.id]
    );
    if (!rows.length) return res.status(404).json({ error: 'Session not found' });
    res.json(rows[0]);
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// Delete session (cascades to files + messages)
app.delete('/sessions/:id', async (req, res) => {
  try {
    const fs   = require('fs');
    const path = require('path');
    const dir  = path.join(process.env.UPLOAD_DIR || '/app/uploads', req.params.id);
    if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true });
    await pool.query('DELETE FROM sessions WHERE id=$1', [req.params.id]);
    res.json({ success: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// Bump last_active + query_count (called by LLM service)
app.patch('/sessions/:id/activity', async (req, res) => {
  try {
    await pool.query(
      `UPDATE sessions SET last_active=NOW(), query_count=query_count+1 WHERE id=$1`,
      [req.params.id]
    );
    res.json({ ok: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/', (_, res) => res.json({ service: 'session-service', status: 'ok' }));
app.get('/health', (_, res) => res.json({ service: 'session-service', status: 'ok', port: PORT }));
app.listen(PORT, '0.0.0.0', () => console.log(`📋 Session Service :${PORT}`));
module.exports = app;
