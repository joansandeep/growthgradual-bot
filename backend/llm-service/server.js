require('dotenv').config();
const express = require('express');
const axios   = require('axios');
const { Pool } = require('pg');

const app  = express();
const PORT = process.env.PORT || process.env.LLM_PORT || 4004;
const SESSION_URL = process.env.SESSION_SERVICE_URL || 'https://paperly-session-service-0vv2.onrender.com';
const RAG_URL     = process.env.RAG_SERVICE_URL     || 'https://sandy31-paperly-rag-service.hf.space';

app.use(express.json({ limit: '50mb' }));

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false,
  max: 5,
});
pool.on('connect', () => console.log('✅ LLM Service DB connected'));
pool.on('error',   e  => console.error('DB pool error:', e.message));

// ═══════════════════════════════════════════════════════════════
// LLM PROVIDERS
// ═══════════════════════════════════════════════════════════════

async function callGemini(sys, user) {
  // Support multiple comma-separated keys for rotation
  const keysStr = process.env.GEMINI_API_KEY || '';
  const keys = keysStr.split(',').map(k => k.trim()).filter(Boolean);
  if (!keys.length) throw new Error('No GEMINI_API_KEY configured');
  // Models tried in order — gemini-2.0-flash is best for free tier
  const models = [
    'gemini-2.0-flash',
    'gemini-2.0-flash-lite',
    'gemini-1.5-flash-8b',
    'gemini-1.5-flash',
  ];
  let lastErr;
  for (const key of keys) {
    for (const m of models) {
      try {
        const url  = `https://generativelanguage.googleapis.com/v1beta/models/${m}:generateContent?key=${key}`;
        const body = {
          system_instruction: { parts: [{ text: sys }] },
          contents:           [{ role: 'user', parts: [{ text: user }] }],
          generationConfig:   { maxOutputTokens: 8192, temperature: 0.1, topP: 0.9 },
        };
        const res  = await axios.post(url, body, { headers: { 'Content-Type': 'application/json' }, timeout: 90000 });
        const text = res.data.candidates?.[0]?.content?.parts?.[0]?.text || '';
        if (!text) throw new Error('Empty response');
        return { text, provider: `Gemini ${m}`, tokens: res.data.usageMetadata?.totalTokenCount || 0 };
      } catch (err) {
        const msg    = err.response?.data?.error?.message || err.message;
        const status = err.response?.status;
        console.warn(`  ⚠️  Gemini ${m} key#${keys.indexOf(key)+1} (${status||'?'}): ${msg.slice(0,120)}`);
        lastErr = err;
        if (status === 429) break; // rate limited on this key — try next key immediately
        if (status === 404) break; // model not found — no point retrying other keys for same model
      }
    }
  }
  throw new Error(`Gemini failed: ${lastErr?.response?.data?.error?.message || lastErr?.message}`);
}

async function callGroq(sys, user) {
  const MAX = 20000;
  const s   = sys.length > MAX ? sys.slice(0, MAX) + '\n\n[...truncated...]' : sys;
  // Support multiple comma-separated keys for rotation
  const keysStr = process.env.GROQ_API_KEY || '';
  const keys = keysStr.split(',').map(k => k.trim()).filter(Boolean);
  if (!keys.length) throw new Error('No GROQ_API_KEY configured');
  const models = [
    'llama-3.3-70b-versatile',      // best quality, free tier
    'llama-3.1-70b-versatile',      // fallback
    'llama3-8b-8192',               // fast small model
    'llama3-70b-8192',              // larger context fallback
  ];
  let lastErr;
  for (const key of keys) {
    for (const m of models) {
      try {
        const res = await axios.post(
          'https://api.groq.com/openai/v1/chat/completions',
          { model:m, messages:[{role:'system',content:s},{role:'user',content:user}], max_tokens:8192, temperature:0.1 },
          { headers:{Authorization:`Bearer ${key}`,'Content-Type':'application/json'}, timeout:90000 }
        );
        return { text:res.data.choices[0].message.content, provider:`Groq ${m}`, tokens:res.data.usage?.total_tokens||0 };
      } catch (err) {
        const status = err.response?.status;
        console.warn(`  ⚠️  Groq ${m} key#${keys.indexOf(key)+1} (${status||'?'}): ${err.response?.data?.error?.message||err.message}`);
        lastErr = err;
        if (status === 429) break;  // rate limited — try next key
        if (status === 404) break;  // model gone — no point retrying
      }
    }
  }
  throw new Error(`Groq failed: ${lastErr?.message}`);
}

async function callOpenRouter(sys, user) {
  const models = [
    'meta-llama/llama-3.3-70b-instruct:free',
    'meta-llama/llama-3.1-8b-instruct:free',
    'mistralai/mistral-7b-instruct:free',
  ];
  let lastErr;
  for (const m of models) {
    try {
      const res = await axios.post(
        'https://openrouter.ai/api/v1/chat/completions',
        { model:m, messages:[{role:'system',content:sys},{role:'user',content:user}], max_tokens:8192, temperature:0.1 },
        { headers:{Authorization:`Bearer ${process.env.OPENROUTER_API_KEY}`,'Content-Type':'application/json','HTTP-Referer':'https://paperly.app','X-Title':'Paperly'}, timeout:90000 }
      );
      const text = res.data.choices?.[0]?.message?.content;
      if (!text) throw new Error('Empty response');
      return { text, provider:`OpenRouter ${m.split('/')[1]}`, tokens:res.data.usage?.total_tokens||0 };
    } catch (err) { console.warn(`  ⚠️  OpenRouter ${m}: ${err.message}`); lastErr = err; }
  }
  throw new Error(`OpenRouter failed: ${lastErr?.message}`);
}

async function callCohere(sys, user) {
  try {
    const res = await axios.post('https://api.cohere.com/v2/chat',
      { model:'command-r-plus-08-2024', messages:[{role:'system',content:sys},{role:'user',content:user}], max_tokens:8192, temperature:0.1 },
      { headers:{Authorization:`Bearer ${process.env.COHERE_API_KEY}`,'Content-Type':'application/json'}, timeout:90000 }
    );
    const text = res.data.message?.content?.[0]?.text||res.data.text||'';
    return { text, provider:'Cohere Command-R+', tokens:res.data.usage?.tokens?.output_tokens||0 };
  } catch {
    const res = await axios.post('https://api.cohere.ai/v1/chat',
      { model:'command-r', preamble:sys, message:user, max_tokens:4096, temperature:0.1 },
      { headers:{Authorization:`Bearer ${process.env.COHERE_API_KEY}`,'Content-Type':'application/json'}, timeout:90000 }
    );
    return { text:res.data.text, provider:'Cohere Command-R', tokens:res.data.meta?.tokens?.output_tokens||0 };
  }
}

const PROVIDERS = {
  gemini:     { fn:callGemini,     key:'GEMINI_API_KEY' },
  groq:       { fn:callGroq,       key:'GROQ_API_KEY' },
  openrouter: { fn:callOpenRouter, key:'OPENROUTER_API_KEY' },
  cohere:     { fn:callCohere,     key:'COHERE_API_KEY' },
};

async function queryLLM(sys, user) {
  const primary  = process.env.PRIMARY_LLM  || 'gemini';
  const fallback = process.env.FALLBACK_LLM || 'groq';
  const available = Object.keys(PROVIDERS).filter(k => process.env[PROVIDERS[k].key]);
  if (!available.length) throw Object.assign(
    new Error('No LLM API keys configured. Add GEMINI_API_KEY or GROQ_API_KEY to .env'),
    { code:'LLM_NOT_CONFIGURED' }
  );
  const order = [...new Set([primary,fallback,...available])].filter(k=>PROVIDERS[k]&&process.env[PROVIDERS[k].key]);
  console.log(`\n🤖 LLM — trying: ${order.join(' → ')}`);
  let lastErr;
  for (const name of order) {
    try {
      const t0  = Date.now();
      const res = await PROVIDERS[name].fn(sys, user);
      res.responseMs = Date.now() - t0;
      console.log(`✅ ${res.provider} — ${res.responseMs}ms`);
      return res;
    } catch (err) { console.warn(`⚠️  ${name}: ${err.message}`); lastErr = err; }
  }
  throw new Error(`All LLM providers failed. Last: ${lastErr?.message}`);
}

// ═══════════════════════════════════════════════════════════════
// RAG HELPERS
// ═══════════════════════════════════════════════════════════════

async function indexToRAG(sessionId, enrichedFiles, pastedTexts) {
  const docs = [];
  for (const f of enrichedFiles) {
    if (f.hasText && (f.extractedText || f.extracted_text)) {
      docs.push({
        id: f.id, name: f.originalName,
        text: f.extractedText || f.extracted_text,
        source_type: 'file', file_type: f.fileType || f.file_type || '',
      });
    }
  }
  for (const p of (pastedTexts || [])) {
    if (p.text?.trim()) {
      docs.push({
        id: p.id || `pasted_${Date.now()}_${Math.random().toString(36).slice(2,8)}`,
        name: p.label || 'Pasted Text',
        text: p.text.trim(), source_type: 'pasted', file_type: 'text',
      });
    }
  }
  if (docs.length > 0) {
    await axios.post(`${RAG_URL}/index`, { session_id:sessionId, documents:docs }, { timeout:30000 });
    console.log(`📚 RAG: Indexed ${docs.length} doc(s) for session ${sessionId.slice(0,8)}`);
  }
  return docs.length;
}

function buildFallbackContext(files, pastedTexts) {
  let total = 0; const MAX = 80000; const parts = [];
  for (const f of files) {
    if (!f.hasText) continue;
    const text = f.extractedText || f.extracted_text || '';
    if (!text) continue;
    const h = `\n\n=== FILE: "${f.originalName}" ===\n`;
    const a = MAX - total - h.length;
    if (a <= 0) break;
    parts.push(`${h}${text.slice(0,a)}`);
    total += h.length + Math.min(text.length, a);
  }
  for (const p of (pastedTexts||[])) {
    if (!p.text?.trim()) continue;
    const h = `\n\n=== PASTED: "${p.label||'Text'}" ===\n`;
    const a = MAX - total - h.length;
    if (a <= 0) break;
    parts.push(`${h}${p.text.trim().slice(0,a)}`);
    total += h.length + a;
  }
  const ctx = parts.join('');
  return {
    systemPrompt: `You are Paperly. Answer ONLY from the provided content. Cite sources. If not found say so.\n\n${ctx}`,
    context: ctx, sourceFiles: files.filter(f=>f.hasText).map(f=>f.originalName),
    retrievedChunks: 0, hasContent: ctx.length > 30, ragUsed: false,
  };
}

// ═══════════════════════════════════════════════════════════════
// ROUTES
// ═══════════════════════════════════════════════════════════════

// ── POST /query — Q&A ────────────────────────────────────────
app.post('/query', async (req, res) => {
  const { sessionId, question, files, pastedTexts } = req.body;
  if (!sessionId || !question)
    return res.status(400).json({ error:'sessionId and question are required' });
  const t0 = Date.now();
  try {
    const fileIds = (files||[]).map(f=>f.id).filter(Boolean);
    let enrichedFiles = files || [];
    if (fileIds.length > 0) {
      const ph = fileIds.map((_,i)=>`$${i+1}`).join(',');
      const { rows } = await pool.query(
        `SELECT id, original_name AS "originalName", file_type AS "fileType",
                has_text AS "hasText", word_count AS "wordCount", page_count AS "pageCount",
                ocr_processed AS "ocrProcessed", extracted_text AS "extractedText",
                processing_error AS "processingError"
         FROM files WHERE id IN (${ph})`, fileIds
      );
      enrichedFiles = rows;
    }
    const validPasted  = (pastedTexts||[]).filter(p=>p.text?.trim());
    const indexedFiles = enrichedFiles.filter(f=>f.hasText);
    const unreadable   = enrichedFiles.filter(f=>!f.hasText);
    const hasAnySrc    = indexedFiles.length > 0 || validPasted.length > 0;

    if (!hasAnySrc) {
      const ans = enrichedFiles.length > 0
        ? `⚠️ **No readable text in uploaded files.**\n\n${unreadable.map(f=>`• ${f.originalName} — ${f.processingError||'No text'}`).join('\n')}\n\nTry re-uploading or pasting text directly.`
        : `⚠️ **No documents uploaded.**\n\nClick 📎 to attach files or paste text, then ask again.`;
      return res.status(200).json({ answer:ans,
        meta:{provider:'none',responseMs:Date.now()-t0,tokensUsed:0,filesAnalyzed:0,ragUsed:false},
        fileStatus:{indexed:[],notIndexed:unreadable.map(f=>({name:f.originalName,reason:f.processingError||'No text'})),pasted:[]} });
    }

    console.log(`\n🔍 Q&A — session=${sessionId.slice(0,8)} q="${question.slice(0,60)}"`);
    let rag;
    try {
      await indexToRAG(sessionId, enrichedFiles, validPasted);
      const qLower = question.toLowerCase();
      // isGeneral: only truly vague/short queries — NOT specific questions about named entities
      const isGeneral = question.split(' ').length <= 5 &&
        ['explain this','summarize this','summarise this','what is this','describe this','overview']
          .some(p=>qLower.includes(p));
      const fileCount = indexedFiles.length;
      // Always pass top_k=0 to let rag_engine's scope detection decide the right value
      // min_score=0 so the rag_engine threshold logic is fully in control
      const ragRes = await axios.post(`${RAG_URL}/query`, {
        session_id:sessionId, question, top_k:0,
        min_score:0.0, file_count:fileCount, is_general:isGeneral,
      }, { timeout:20000 });
      rag = { systemPrompt:ragRes.data.grounded_system_prompt, context:ragRes.data.context,
              sourceFiles:ragRes.data.source_files, retrievedChunks:ragRes.data.retrieved_chunks,
              hasContent:ragRes.data.has_content, ragUsed:true };
    } catch (ragErr) {
      console.warn(`⚠️  RAG unavailable: ${ragErr.message} — using fallback`);
      rag = buildFallbackContext(enrichedFiles, validPasted);
    }

    const llmRes = await queryLLM(rag.systemPrompt, question);
    const ms = Date.now() - t0;
    await pool.query(`INSERT INTO messages (session_id,role,content) VALUES ($1,'user',$2)`, [sessionId,question]);
    await pool.query(`INSERT INTO messages (session_id,role,content,llm_provider,tokens_used,response_ms) VALUES ($1,'assistant',$2,$3,$4,$5)`,
      [sessionId,llmRes.text,llmRes.provider,llmRes.tokens||0,ms]);
    axios.patch(`${SESSION_URL}/sessions/${sessionId}/activity`).catch(()=>{});

    res.json({
      answer: llmRes.text,
      meta:{ provider:llmRes.provider, responseMs:ms, tokensUsed:llmRes.tokens,
             filesAnalyzed:indexedFiles.length, pastedCount:validPasted.length,
             totalFiles:enrichedFiles.length, retrievedChunks:rag.retrievedChunks, ragUsed:rag.ragUsed },
      fileStatus:{ indexed:indexedFiles.map(f=>f.originalName),
                   notIndexed:unreadable.map(f=>({name:f.originalName,reason:f.processingError||'No text'})),
                   pasted:validPasted.map(p=>p.label||'Pasted text'), sources:rag.sourceFiles },
    });
  } catch (err) {
    console.error('[query error]', err.message);
    const status = err.code==='LLM_NOT_CONFIGURED' ? 503 : 500;
    res.status(status).json({ error:err.message, code:err.code||'QUERY_FAILED' });
  }
});

// ── POST /report — Generate Report ───────────────────────────
app.post('/report', async (req, res) => {
  const { sessionId, reportSpec = 'Generate a comprehensive report based on the documents.', reportType = 'comprehensive', files, pastedTexts } = req.body;
  if (!sessionId)
    return res.status(400).json({ error:'sessionId is required' });
  const t0 = Date.now();
  try {
    // Enrich files
    const fileIds = (files||[]).map(f=>f.id).filter(Boolean);
    let enrichedFiles = files || [];
    if (fileIds.length > 0) {
      const ph = fileIds.map((_,i)=>`$${i+1}`).join(',');
      const { rows } = await pool.query(
        `SELECT id, original_name AS "originalName", file_type AS "fileType",
                has_text AS "hasText", word_count AS "wordCount", page_count AS "pageCount",
                ocr_processed AS "ocrProcessed", extracted_text AS "extractedText",
                processing_error AS "processingError"
         FROM files WHERE id IN (${ph})`, fileIds
      );
      enrichedFiles = rows;
    }
    const validPasted  = (pastedTexts||[]).filter(p=>p.text?.trim());
    const indexedFiles = enrichedFiles.filter(f=>f.hasText);
    const hasAnySrc    = indexedFiles.length > 0 || validPasted.length > 0;

    if (!hasAnySrc) {
      // Files exist but none have extractable text
      const unreadable = enrichedFiles.filter(f => !f.hasText);
      const ans = enrichedFiles.length > 0
        ? `⚠️ **No readable text in uploaded files.**\n\n${unreadable.map(f => `• ${f.originalName} — ${f.processingError || 'No text extracted'}`).join('\n')}\n\nTry re-uploading, or paste the text content directly into the chat.`
        : '⚠️ **No documents uploaded.** Please attach files or paste text before generating a report.';
      return res.status(200).json({ answer: ans, meta: { provider: 'none', responseMs: 0, tokensUsed: 0 } });
    }

    console.log(`\n📄 Report — session=${sessionId.slice(0,8)} type="${reportType}"`);
    let rag;
    try {
      await indexToRAG(sessionId, enrichedFiles, validPasted);
      const ragRes = await axios.post(`${RAG_URL}/report`, {
        session_id:sessionId, report_spec:reportSpec, report_type:reportType,
      }, { timeout:30000 });
      rag = { systemPrompt:ragRes.data.grounded_system_prompt, sourceFiles:ragRes.data.source_files,
              retrievedChunks:ragRes.data.retrieved_chunks, hasContent:ragRes.data.has_content, ragUsed:true };
    } catch (ragErr) {
      console.warn(`⚠️  RAG unavailable for report: ${ragErr.message} — using fallback`);
      rag = buildFallbackContext(enrichedFiles, validPasted);
    }

    // Use higher token limit for reports
    const llmRes = await queryLLM(rag.systemPrompt, `Generate the report as specified: ${reportSpec}`);
    const ms = Date.now() - t0;

    // Save as assistant message with special marker
    await pool.query(
      `INSERT INTO messages (session_id,role,content,llm_provider,tokens_used,response_ms) VALUES ($1,'assistant',$2,$3,$4,$5)`,
      [sessionId, llmRes.text, llmRes.provider, llmRes.tokens||0, ms]
    );

    res.json({
      report: llmRes.text,
      reportType,
      meta:{ provider:llmRes.provider, responseMs:ms, tokensUsed:llmRes.tokens,
             filesAnalyzed:indexedFiles.length, retrievedChunks:rag.retrievedChunks, ragUsed:rag.ragUsed },
      sourceFiles: rag.sourceFiles,
    });
  } catch (err) {
    console.error('[report error]', err.message);
    const status = err.code==='LLM_NOT_CONFIGURED' ? 503 : 500;
    res.status(status).json({ error:err.message, code:err.code||'REPORT_FAILED' });
  }
});

// ── POST /index (called externally) ─────────────────────────
app.post('/index', async (req, res) => {
  const { sessionId, documents } = req.body;
  try {
    const r = await axios.post(`${RAG_URL}/index`, { session_id:sessionId, documents }, { timeout:30000 });
    res.json(r.data);
  } catch (err) { res.json({ chunks_added:0, total_chunks:0, ragAvailable:false }); }
});

// ── GET /query/history ───────────────────────────────────────
app.get('/query/history/:sessionId', async (req, res) => {
  try {
    const { rows } = await pool.query(
      `SELECT id,role,content,llm_provider,tokens_used,response_ms,created_at
       FROM messages WHERE session_id=$1 ORDER BY created_at ASC LIMIT 100`,
      [req.params.sessionId]
    );
    res.json({ messages:rows, count:rows.length });
  } catch (e) { res.status(500).json({ error:e.message }); }
});

app.get('/', (_, res) => res.json({ service: 'llm-service', status: 'ok' }));
app.get('/health', (_, res) => {
  const configured = Object.keys(PROVIDERS).filter(k=>process.env[PROVIDERS[k].key]);
  res.json({ service:'llm-service', status:'ok', port:PORT,
    llm:{configured, primary:process.env.PRIMARY_LLM||'gemini', fallback:process.env.FALLBACK_LLM||'groq', ready:configured.length>0},
    rag:{url:RAG_URL} });
});

app.use((err,req,res,next) => res.status(500).json({ error:err.message }));
app.listen(PORT, '0.0.0.0', () => console.log(`🤖 LLM Service :${PORT} | RAG: ${RAG_URL}`));
module.exports = app;
