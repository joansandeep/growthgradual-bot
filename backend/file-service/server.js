require('dotenv').config();
const express  = require('express');
const multer   = require('multer');
const path     = require('path');
const fs       = require('fs');
const axios    = require('axios');
const { v4: uuidv4 } = require('uuid');
const { createClient } = require('@supabase/supabase-js');
const { connectDB, ensureSchema, pool } = require('./config/db');

const app  = express();
const PORT = process.env.PORT || process.env.FILE_PORT || 4002;
const EXTRACTION_URL = process.env.EXTRACTION_SERVICE_URL || 'https://paperly-extraction-service-l9nr.onrender.com';
const MAX_FILES = parseInt(process.env.MAX_FILES) || 15;

// FIX: Use service_role key for DB writes (anon key has RLS restrictions)
// SUPABASE_ANON_KEY in .env must be the long eyJ... JWT token from
// Supabase Dashboard → Settings → API → "anon public" key
const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_ANON_KEY
);

app.use(express.json());
connectDB()
  .then(ensureSchema)
  .catch(e => console.error('DB init error (non-fatal):', e.message));

const ALLOWED_MIME = {
  'application/pdf': 'pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'application/msword': 'docx',
  'image/jpeg': 'image', 'image/jpg': 'image',
  'image/png': 'image', 'image/webp': 'image',
};

const upload = multer({
  storage: multer.memoryStorage(),
  fileFilter: (req, file, cb) => {
    const mime = file.mimetype.toLowerCase();
    const ext  = path.extname(file.originalname).toLowerCase();
    if (ALLOWED_MIME[mime] || ['.pdf','.docx','.doc','.jpg','.jpeg','.png','.webp'].includes(ext)) {
      cb(null, true);
    } else {
      cb(new multer.MulterError('LIMIT_UNEXPECTED_FILE', `Unsupported type: ${mime}`));
    }
  },
  limits: { fileSize: (parseInt(process.env.MAX_FILE_SIZE_MB)||50)*1024*1024, files: MAX_FILES },
});

// ── POST /files/upload/:sessionId ─────────────────────────
app.post('/files/upload/:sessionId', (req, res, next) => {
  upload.array('files', MAX_FILES)(req, res, err => {
    if (err instanceof multer.MulterError) {
      return res.status(400).json({ error: err.message });
    }
    if (err) return next(err);
    next();
  });
}, async (req, res) => {
  const { sessionId } = req.params;
  const uploaded = req.files || [];
  if (!uploaded.length) return res.status(400).json({ error: 'No files received' });

  // FIX: Ensure session row exists — upsert so it works even if session
  // was created by frontend UUID (not yet in DB)
  try {
    await pool.query(
      `INSERT INTO sessions (id) VALUES ($1) ON CONFLICT (id) DO UPDATE SET last_active=NOW()`,
      [sessionId]
    );
  } catch (sessionErr) {
    console.error('Session upsert error:', sessionErr.message);
    // Try a plain insert ignoring conflict
    try {
      await pool.query(`INSERT INTO sessions (id) VALUES ($1) ON CONFLICT DO NOTHING`, [sessionId]);
    } catch (e2) {
      return res.status(500).json({ error: `Could not create session: ${e2.message}` });
    }
  }

  // Check file count limit
  let currentCount = 0;
  try {
    const { rows: [{ count }] } = await pool.query(
      'SELECT COUNT(*) FROM files WHERE session_id=$1', [sessionId]
    );
    currentCount = parseInt(count);
  } catch (e) {
    console.warn('Count check failed:', e.message);
  }

  if (currentCount + uploaded.length > MAX_FILES) {
    return res.status(400).json({ error: `Session already has ${currentCount} files. Max is ${MAX_FILES}.` });
  }

  const results = [];
  let indexed = 0, errors = 0;

  for (const file of uploaded) {
    try {
      // 1. Upload from memory to Supabase storage
      const extension = path.extname(file.originalname).toLowerCase();
      const fileName = `${uuidv4()}${extension}`;
      const bucketPath = `${sessionId}/${fileName}`;

      console.log(`📤 Uploading ${file.originalname} to Supabase storage: ${bucketPath}`);
      const { data: supaData, error: supaErr } = await supabase.storage
        .from('paperly-uploads')
        .upload(bucketPath, file.buffer, {
          contentType: file.mimetype,
          upsert: false
        });

      if (supaErr) throw new Error(`Supabase storage upload failed: ${supaErr.message}`);
      console.log(`✅ Storage upload OK: ${supaData.path}`);

      // 2. Get public URL
      const { data: { publicUrl } } = supabase.storage
        .from('paperly-uploads')
        .getPublicUrl(bucketPath);

      console.log(`🔗 Public URL: ${publicUrl}`);

      // 3. Call extraction service
      let ext = {
        fileType: ALLOWED_MIME[file.mimetype] || 'unsupported',
        text: '', hasText: false, ocrProcessed: false,
        wordCount: 0, pageCount: null, error: null
      };
      try {
        console.log(`🔍 Calling extraction: ${EXTRACTION_URL}/extract`);
        const er = await axios.post(`${EXTRACTION_URL}/extract`, {
          fileUrl: publicUrl,
          originalName: file.originalname,
          mimetype: file.mimetype
        }, { timeout: 120000 });
        ext = er.data;
        console.log(`✅ Extraction done: hasText=${ext.hasText} words=${ext.wordCount}`);
      } catch (extractErr) {
        console.error(`Extraction failed for ${file.originalname}:`, extractErr.message);
        ext.error = `Extraction failed: ${extractErr.message}`;
      }

      // 4. Insert into files table
      console.log(`💾 Inserting file record for session ${sessionId}`);
      const { rows } = await pool.query(
        `INSERT INTO files
           (session_id, original_name, stored_name, mime_type, file_type,
            size_bytes, extracted_text, has_text, ocr_processed,
            word_count, page_count, processing_error)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
         RETURNING id, original_name, file_type, size_bytes,
                   has_text, ocr_processed, word_count, page_count, processing_error, created_at`,
        [
          sessionId, file.originalname, publicUrl, file.mimetype,
          ext.fileType || 'unsupported', file.size,
          ext.text || '', ext.hasText || false, ext.ocrProcessed || false,
          ext.wordCount || 0, ext.pageCount || null, ext.error || null
        ]
      );
      const f = rows[0];
      console.log(`✅ File record saved: ${f.id}`);
      if (f.has_text) indexed++;
      results.push({
        id: f.id, originalName: f.original_name, fileType: f.file_type,
        size: parseInt(f.size_bytes), hasText: f.has_text,
        ocrProcessed: f.ocr_processed, wordCount: f.word_count,
        pageCount: f.page_count, processingError: f.processing_error,
        createdAt: f.created_at,
        status: f.has_text ? 'indexed' : 'no_text',
        message: f.has_text
          ? `${f.word_count?.toLocaleString()} words extracted`
          : (f.processing_error || 'No text content'),
      });
    } catch (err) {
      console.error(`Error processing ${file.originalname}:`, err.message);
      errors++;
      results.push({ originalName: file.originalname, status: 'error', message: err.message });
    }
  }

  res.json({ success: true, files: results, summary: { total: results.length, indexed, errors } });
});

// ── GET /files/:sessionId ──────────────────────────────────
app.get('/files/:sessionId', async (req, res) => {
  try {
    const { rows } = await pool.query(
      `SELECT id, original_name, file_type, size_bytes, has_text,
              ocr_processed, word_count, page_count, processing_error, extracted_text, created_at
       FROM files WHERE session_id=$1 ORDER BY created_at ASC`,
      [req.params.sessionId]
    );
    const files = rows.map(f => ({
      id: f.id, originalName: f.original_name, fileType: f.file_type,
      size: parseInt(f.size_bytes), hasText: f.has_text,
      ocrProcessed: f.ocr_processed, wordCount: f.word_count,
      pageCount: f.page_count, processingError: f.processing_error,
      extractedText: f.extracted_text, createdAt: f.created_at,
    }));
    res.json({
      files, total: files.length, maxFiles: MAX_FILES,
      indexed: files.filter(f=>f.hasText).length,
      totalWords: files.reduce((a,f)=>a+(f.wordCount||0),0),
    });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── DELETE /files/:sessionId/:fileId ─────────────────────
app.delete('/files/:sessionId/:fileId', async (req, res) => {
  try {
    const { rows } = await pool.query(
      'SELECT stored_name FROM files WHERE id=$1 AND session_id=$2',
      [req.params.fileId, req.params.sessionId]
    );
    if (!rows.length) return res.status(404).json({ error: 'File not found' });

    const publicUrl = rows[0].stored_name;
    const pathParts = publicUrl.split('paperly-uploads/');
    if (pathParts.length > 1) {
      await supabase.storage.from('paperly-uploads').remove([pathParts[1]]);
    }

    await pool.query('DELETE FROM files WHERE id=$1', [req.params.fileId]);
    res.json({ success: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── DELETE /files/:sessionId — clear all ──────────────────
app.delete('/files/:sessionId', async (req, res) => {
  try {
    const { rows } = await pool.query('SELECT stored_name FROM files WHERE session_id=$1', [req.params.sessionId]);
    const filesToRemove = rows.map(r => r.stored_name.split('paperly-uploads/')[1]).filter(Boolean);

    if (filesToRemove.length > 0) {
      await supabase.storage.from('paperly-uploads').remove(filesToRemove);
    }

    const { rowCount } = await pool.query('DELETE FROM files WHERE session_id=$1', [req.params.sessionId]);
    res.json({ success: true, deleted: rowCount });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── POST /files/:sessionId/:fileId/reextract ─────────────
app.post('/files/:sessionId/:fileId/reextract', async (req, res) => {
  try {
    const { rows } = await pool.query(
      'SELECT * FROM files WHERE id=$1 AND session_id=$2',
      [req.params.fileId, req.params.sessionId]
    );
    if (!rows.length) return res.status(404).json({ error: 'File not found' });

    const file = rows[0];
    let ext = {
      fileType: file.file_type, text: '', hasText: false,
      ocrProcessed: false, wordCount: 0, pageCount: file.page_count, error: null
    };

    try {
      const er = await axios.post(`${EXTRACTION_URL}/extract`, {
        fileUrl: file.stored_name,
        originalName: file.original_name,
        mimetype: file.mime_type
      }, { timeout: 120000 });
      ext = er.data;
    } catch (extractErr) {
      ext.error = `Re-extraction failed: ${extractErr.message}`;
    }

    await pool.query(
      `UPDATE files SET file_type=$1, extracted_text=$2, has_text=$3,
       ocr_processed=$4, word_count=$5, page_count=$6, processing_error=$7
       WHERE id=$8`,
      [
        ext.fileType || file.file_type, ext.text || '', ext.hasText || false,
        ext.ocrProcessed || false, ext.wordCount || 0, ext.pageCount || file.page_count,
        ext.error || null, req.params.fileId
      ]
    );

    res.json({
      success: true,
      hasText: ext.hasText,
      wordCount: ext.wordCount || 0,
      error: ext.error || null,
      message: ext.hasText
        ? `${ext.wordCount?.toLocaleString()} words extracted`
        : (ext.error || 'No text found'),
    });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/', (_, res) => res.json({ service: 'file-service', status: 'ok' }));
app.get('/health', (_, res) => res.json({
  service: 'file-service', status: 'ok', port: PORT,
  supabase: !!process.env.SUPABASE_URL,
  database: !!process.env.DATABASE_URL,
}));
app.use((err, req, res, next) => res.status(500).json({ error: err.message }));
app.listen(PORT, '0.0.0.0', () => console.log(`📁 File Service :${PORT}`));
module.exports = app;
