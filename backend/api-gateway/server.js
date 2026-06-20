require('dotenv').config();
const express    = require('express');
const cors       = require('cors');
const helmet     = require('helmet');
const morgan     = require('morgan');
const compression = require('compression');
const rateLimit  = require('express-rate-limit');
const axios      = require('axios');
const multer     = require('multer');
const FormData   = require('form-data');
const path       = require('path');

const app  = express();
const PORT = process.env.PORT || 4000;

const SERVICES = {
  session:    process.env.SESSION_SERVICE_URL    || 'https://paperly-session-service-0vv2.onrender.com',
  file:       process.env.FILE_SERVICE_URL       || 'https://paperly-file-service-jkyd.onrender.com',
  extraction: process.env.EXTRACTION_SERVICE_URL || 'https://paperly-extraction-service-l9nr.onrender.com',
  llm:        process.env.LLM_SERVICE_URL        || 'https://paperly-llm-service-xqi1.onrender.com',
  rag:        process.env.RAG_SERVICE_URL        || 'https://sandy31-paperly-rag-service.hf.space',
};

app.use(compression());
app.use(helmet({ crossOriginResourcePolicy: false, contentSecurityPolicy: false, frameguard: false }));
app.use('/static', express.static(path.join(__dirname, 'public')));
app.get('/', (req, res) => res.send('Paperly API Gateway is running! 🌐'));

app.use(cors({
  origin: (origin, callback) => {
    if (!origin || process.env.NODE_ENV !== 'production') return callback(null, true);
    const allowed = [
      process.env.FRONTEND_URL || 'https://paperly-frontend.onrender.com',
      /\.onrender\.com$/, /\.up\.railway\.app$/, /\.vercel\.app$/,
      /^https?:\/\/192\.168\.\d+\.\d+(:\d+)?$/,
      /^https?:\/\/10\.\d+\.\d+\.\d+(:\d+)?$/,
      /^https?:\/\/172\.(1[6-9]|2\d|3[01])\.\d+\.\d+(:\d+)?$/,
    ];
    const ok = allowed.some(o => typeof o === 'string' ? o === origin : o.test(origin));
    callback(ok ? null : new Error(`CORS blocked: ${origin}`), ok);
  },
  credentials: true,
}));

app.use(morgan(process.env.NODE_ENV === 'production' ? 'combined' : 'dev'));
app.use('/api/', rateLimit({ windowMs: 15*60*1000, max: 300, standardHeaders: true }));
app.use('/api/query', rateLimit({ windowMs: 60*1000, max: 30 }));
app.use('/api/files/upload', rateLimit({ windowMs: 60*1000, max: 20 }));
app.use(express.json({ limit: '20mb' }));
app.use(express.urlencoded({ extended: true }));

const upload = multer({
  storage: multer.memoryStorage(),
  limits: {
    fileSize: (parseInt(process.env.MAX_FILE_SIZE_MB) || 50) * 1024 * 1024,
    files: 15,
  },
});

async function proxy(serviceUrl, method, urlPath, data, headers = {}) {
  const response = await axios({
    method, url: `${serviceUrl}${urlPath}`, data, headers,
    timeout: 120000, validateStatus: () => true,
  });
  return response;
}

// ═══ SESSION ROUTES ═══
app.post('/api/sessions', async (req, res) => {
  try {
    const r = await proxy(SERVICES.session, 'post', '/sessions', req.body);
    res.status(r.status).json(r.data);
  } catch (err) { res.status(503).json({ error: 'Session service unavailable: ' + err.message }); }
});
app.get('/api/sessions/:id', async (req, res) => {
  try {
    const r = await proxy(SERVICES.session, 'get', `/sessions/${req.params.id}`);
    res.status(r.status).json(r.data);
  } catch (err) { res.status(503).json({ error: 'Session service unavailable: ' + err.message }); }
});
app.delete('/api/sessions/:id', async (req, res) => {
  try {
    const r = await proxy(SERVICES.session, 'delete', `/sessions/${req.params.id}`);
    res.status(r.status).json(r.data);
  } catch (err) { res.status(503).json({ error: 'Session service unavailable: ' + err.message }); }
});

// ═══ FILE ROUTES ═══
app.post('/api/files/upload/:sessionId',
  upload.array('files', 15),
  async (req, res) => {
    try {
      const fd = new FormData();
      (req.files || []).forEach(f => {
        fd.append('files', f.buffer, {
          filename: f.originalname, contentType: f.mimetype,
        });
      });
      const r = await axios.post(
        `${SERVICES.file}/files/upload/${req.params.sessionId}`,
        fd, { headers: fd.getHeaders(), timeout: 300000, maxContentLength: Infinity, maxBodyLength: Infinity }
      );
      res.status(r.status).json(r.data);
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  }
);
app.get('/api/files/:sessionId', async (req, res) => {
  try {
    const r = await proxy(SERVICES.file, 'get', `/files/${req.params.sessionId}`);
    res.status(r.status).json(r.data);
  } catch (err) { res.status(503).json({ error: 'File service unavailable: ' + err.message }); }
});
app.delete('/api/files/:sessionId/:fileId', async (req, res) => {
  try {
    const r = await proxy(SERVICES.file, 'delete', `/files/${req.params.sessionId}/${req.params.fileId}`);
    res.status(r.status).json(r.data);
  } catch (err) { res.status(503).json({ error: 'File service unavailable: ' + err.message }); }
});
app.delete('/api/files/:sessionId', async (req, res) => {
  try {
    const r = await proxy(SERVICES.file, 'delete', `/files/${req.params.sessionId}`);
    res.status(r.status).json(r.data);
  } catch (err) { res.status(503).json({ error: 'File service unavailable: ' + err.message }); }
});

// ═══ RE-EXTRACT ROUTE — retries extraction for a file ═══
app.post('/api/files/:sessionId/:fileId/reextract', async (req, res) => {
  try {
    const r = await proxy(SERVICES.file, 'post', `/files/${req.params.sessionId}/${req.params.fileId}/reextract`);
    res.status(r.status).json(r.data);
  } catch (err) { res.status(503).json({ error: 'File service unavailable: ' + err.message }); }
});

// ═══ QUERY ROUTE ═══
app.post('/api/query', async (req, res) => {
  try {
    const { sessionId, question, pastedTexts } = req.body;
    if (!sessionId || !question)
      return res.status(400).json({ error: 'sessionId and question are required' });

    // Fetch files
    let files = [];
    try {
      const fr = await proxy(SERVICES.file, 'get', `/files/${sessionId}`);
      if (fr.status === 200) files = fr.data.files || [];
    } catch (e) {
      console.warn('[GW] Could not fetch files:', e.message);
    }

    const hasPasted = Array.isArray(pastedTexts) && pastedTexts.some(p => p.text?.trim());
    const hasFiles  = files.length > 0;

    // No content at all — return 200 with a helpful answer (not 400, so frontend shows it nicely)
    if (!hasFiles && !hasPasted) {
      return res.status(200).json({
        answer: '⚠️ **No documents uploaded yet.**\n\nPlease:\n1. Click **📎** to attach files (PDF, DOCX, images)\n2. Or paste text directly into the input box\n\nThen ask your question again.',
        meta: { provider: 'none', responseMs: 0, tokensUsed: 0, filesAnalyzed: 0, ragUsed: false },
        fileStatus: { indexed: [], notIndexed: [], pasted: [] },
      });
    }

    // Forward to LLM service
    const r = await proxy(
      SERVICES.llm, 'post', '/query',
      { sessionId, question, files, pastedTexts: pastedTexts || [] },
      { 'Content-Type': 'application/json' }
    );

    // Surface any error from LLM service as a readable chat message, not a raw error
    if (r.status >= 400) {
      const errMsg = r.data?.error || r.data?.message || 'LLM service error (' + r.status + ')';
      return res.status(200).json({
        answer: '⚠️ **' + errMsg + '**\n\nPlease try again in a few seconds.',
        meta: { provider: 'error', responseMs: 0, tokensUsed: 0, filesAnalyzed: 0, ragUsed: false },
        fileStatus: { indexed: [], notIndexed: [], pasted: [] },
      });
    }

    res.status(r.status).json(r.data);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/query/history/:sessionId', async (req, res) => {
  try {
    const r = await proxy(SERVICES.llm, 'get', `/query/history/${req.params.sessionId}`);
    res.status(r.status).json(r.data);
  } catch (err) {
    console.error('[GW] history proxy error:', err.message);
    res.status(503).json({ error: 'LLM service unavailable — it may be waking up. Try again in a moment.', messages: [], count: 0 });
  }
});

// ═══ REPORT ROUTE ═══
app.post('/api/report', async (req, res) => {
  try {
    const { sessionId, reportSpec = 'Generate a comprehensive report based on the documents.', reportType, pastedTexts } = req.body;
    if (!sessionId)
      return res.status(400).json({ error: 'sessionId is required' });

    let files = [];
    try {
      const fr = await proxy(SERVICES.file, 'get', `/files/${sessionId}`);
      if (fr.status === 200) files = fr.data.files || [];
    } catch (e) {
      console.warn('[GW] Could not fetch files for report:', e.message);
    }

    const hasPasted = Array.isArray(pastedTexts) && pastedTexts.some(p => p.text?.trim());
    if (!files.length && !hasPasted) {
      return res.status(200).json({
        answer: '⚠️ **No documents uploaded yet.**\n\nPlease attach files or paste text before generating a report.',
        meta: { provider: 'none', responseMs: 0, tokensUsed: 0 },
      });
    }

    const r = await proxy(
      SERVICES.llm, 'post', '/report',
      { sessionId, reportSpec, reportType, files, pastedTexts: pastedTexts || [] },
      { 'Content-Type': 'application/json' }
    );

    if (r.status === 400 && r.data?.answer) {
      return res.status(200).json(r.data);
    }

    res.status(r.status).json(r.data);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ═══ HEALTH ═══
app.get('/api/health', async (req, res) => {
  const checks = await Promise.allSettled([
    axios.get(`${SERVICES.session}/health`,    { timeout: 3000 }),
    axios.get(`${SERVICES.file}/health`,       { timeout: 3000 }),
    axios.get(`${SERVICES.extraction}/health`, { timeout: 3000 }),
    axios.get(`${SERVICES.llm}/health`,        { timeout: 3000 }),
    axios.get(`${SERVICES.rag}/health`,        { timeout: 5000 }),
  ]);
  const names = ['session', 'file', 'extraction', 'llm', 'rag'];
  const results = {};
  checks.forEach((c, i) => {
    results[names[i]] = c.status === 'fulfilled'
      ? { ok: true, ...c.value.data }
      : { ok: false, error: c.reason?.message };
  });
  const allOk = Object.values(results).every(s => s.ok);
  res.status(allOk ? 200 : 207).json({
    status:   allOk ? 'healthy' : 'degraded',
    gateway:  `api-gateway:${PORT}`,
    services: results,
    uptime:   Math.floor(process.uptime()),
  });
});

app.get('/api', (_, res) => res.json({ name: 'Paperly API Gateway v4', status: 'ok', port: PORT }));
app.use((err, req, res, next) => { console.error('[GW Error]', err.message); res.status(500).json({ error: err.message }); });
app.use((req, res) => res.status(404).json({ error: `${req.method} ${req.path} not found` }));

app.listen(PORT, '0.0.0.0', () => {
  console.log(`\n🌐 API Gateway running on :${PORT} (all interfaces)`);
  console.log('Services:', JSON.stringify(SERVICES, null, 2));
});

module.exports = app;
