'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import Image from 'next/image';

// ─── Types ────────────────────────────────────────────────────────────────────
interface Source { title: string; url: string; snippet: string; }
interface ChartDataPoint { label: string; value: number; }
interface ChartSeries { name: string; data: ChartDataPoint[]; color?: string; }
/** Route a Tavily/third-party image URL through our server-side proxy to bypass hotlink protection. */
function proxyImg(url: string): string {
  if (!url) return url;
  if (url.startsWith('/') || url.startsWith('data:')) return url;
  return `/api/image-proxy?url=${encodeURIComponent(url)}`;
}

interface DatawrapperInfo { id: string; embedUrl: string; publicUrl: string; }
interface ChartSpec { type: 'bar' | 'line' | 'pie' | 'table'; title: string; series?: ChartSeries[]; unit?: string; columns?: string[]; rows?: string[][]; datawrapper?: DatawrapperInfo; }
interface WebImage { url: string; caption?: string; }
interface ReportData { report: string; title?: string; charts: ChartSpec[]; images?: WebImage[]; keyStats: {label:string;value:string;change?:string}[]; summary: string; fileImages?: {name:string;mimeType:string;data:string}[]; sourceDocuments?: {name:string;text:string;file_type?:string}[]; }
interface Message {
  id: string; role: 'user' | 'assistant'; text: string; ts: number;
  sources?: Source[]; searchPerformed?: boolean; queryType?: string;
  reportData?: ReportData; reportLoading?: boolean;
  inlineCharts?: ChartSpec[]; wantsVisual?: boolean;
  // Report is no longer auto-generated — these carry what's needed so the
  // "Generate Report" button can build the request whenever the user taps it.
  reportEligible?: boolean; reportQuestion?: string; reportFiles?: AttachedFile[];
  followUpQuestions?: string[];
}
interface Conversation {
  id: string; title: string; messages: Message[]; ts: number;
}

function uid() { return Math.random().toString(36).slice(2, 10); }
function fmtTime(ts: number) {
  return new Date(ts).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
}
function fmtDate(ts: number) {
  const d = new Date(ts);
  const today = new Date();
  const diff = today.getDate() - d.getDate();
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

// ─── Storage helpers ──────────────────────────────────────────────────────────
const STORAGE_KEY = 'growth_gradual_conversations';
const SESSION_ID_KEY = 'growth_gradual_session_id';

/**
 * Module-level fallback for environments where localStorage is unavailable
 * (incognito/private mode on Safari/Firefox, SSR, blocked storage).
 *
 * CRITICAL: must be module-level so every call within the same page load
 * returns the SAME UUID even when localStorage is blocked.
 *
 * Without this, incognito mode returns a fresh UUID on every call:
 *   - File upload indexes under UUID "A"
 *   - Chat message sends sessionId "B" → RAG finds nothing, file ignored
 */
let _inMemorySessionId: string | null = null;

/** Return a stable UUID for this browser tab/page-load.
 *  Priority: localStorage (persists across reloads) → in-memory (stable for
 *  this page load, works in incognito/private/Safari/Firefox strict mode). */
function getOrCreateSessionId(): string {
  // 1. Try localStorage — persists across browser reloads (normal mode)
  try {
    const existing = localStorage.getItem(SESSION_ID_KEY);
    if (existing) {
      _inMemorySessionId = existing; // keep in-memory copy in sync
      return existing;
    }
    const id = crypto.randomUUID();
    localStorage.setItem(SESSION_ID_KEY, id);
    _inMemorySessionId = id;
    return id;
  } catch {
    // localStorage blocked (incognito, Safari ITP, Firefox strict, SSR)
  }

  // 2. In-memory fallback — stable for this page load even in incognito
  if (!_inMemorySessionId) {
    _inMemorySessionId = crypto.randomUUID();
  }
  return _inMemorySessionId;
}
function loadConversations(): Conversation[] {
  if (typeof window === 'undefined') return [];
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]'); } catch { return []; }
}
function saveConversations(convs: Conversation[]) {
  if (typeof window === 'undefined') return;
  // Strip reportLoading flag before persisting — a loading state in a saved conversation
  // would re-trigger the report spinner with no active fetch on reload.
  // Also drop reportFiles (raw base64 attachments) — keeping these out of
  // localStorage avoids bloating it; reportEligible/reportQuestion are kept
  // so the "Generate Report" button still works after a reload (just
  // without the original attachments).
  const cleaned = convs.slice(0, 50).map(c => ({
    ...c,
    messages: c.messages.map(m => (m.reportLoading || m.reportFiles)
      ? { ...m, reportLoading: false, reportFiles: undefined }
      : m),
  }));
  localStorage.setItem(STORAGE_KEY, JSON.stringify(cleaned));
}

// ─── Markdown renderer ────────────────────────────────────────────────────────
function renderMd(text: string): string {
  return text
    // Normalize line endings, strip trailing spaces, and collapse runs of 3+
    // blank lines (common in LLM output) down to a single blank line so we
    // don't end up stacking extra empty paragraphs / gaps before tables etc.
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+$/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
    .replace(/^### (.+)$/gm, '<h3 class="md-h3">$1</h3>')
    .replace(/^## (.+)$/gm,  '<h2 class="md-h2">$1</h2>')
    .replace(/^# (.+)$/gm,   '<h1 class="md-h1">$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code class="md-code">$1</code>')
    // Collapse any blank line(s) sitting directly above a table into a
    // single newline, so the paragraph-break logic below doesn't insert a
    // stray empty <p> right before the <table> (the line break itself must
    // stay, since the per-line table-row regex below needs each row to
    // start at the beginning of a line).
    .replace(/\n{2,}(?=\|.+\|\n\|[\s|:-]+\|)/g, '\n')
    .replace(/^\|(.+)\|$/gm, (row) => {
      if (/^[\s|:-]+$/.test(row)) return '<!--sep-->';
      const cells = row.split('|').filter(Boolean).map(c => `<td class="md-td">${c.trim()}</td>`).join('');
      return `<tr>${cells}</tr>`;
    })
    .replace(
      /((?:<tr>.*?<\/tr>\n?))(<!--sep-->\n)?((?:<tr>.*?<\/tr>\n?)*)/gs,
      (_: string, firstRow: string, sep: string, restRows: string) => {
        if (!firstRow.trim()) return _;
        // The optional trailing \n? in each repeated group gets captured as
        // part of the group text itself (regex groups don't "consume and
        // discard" — whatever they match is included). Strip those here so
        // they don't survive into the table HTML and later get turned into
        // stray <br/> tags by the \n → <br/> pass below.
        const cleanFirst = firstRow.replace(/\n/g, '');
        const cleanRest = restRows.replace(/\n/g, '');
        if (sep) {
          const head = cleanFirst.replace(/<td class="md-td">/g, '<th class="md-th">').replace(/<\/td>/g, '<\/th>');
          return `<table class="md-table"><thead>${head}<\/thead><tbody>${cleanRest}<\/tbody><\/table>`;
        }
        return `<table class="md-table"><tbody>${cleanFirst}${cleanRest}<\/tbody><\/table>`;
      }
    )
    .replace(/^\s*[-*+]\s+(.+)$/gm, '<li class="md-li">$1</li>')
    .replace(/(<li[\s\S]*?<\/li>\n?)+/g, m => `<ul class="md-ul">${m}</ul>`)
    .replace(/\[(\d+)\]/g, '<sup class="md-ref">[$1]</sup>')
    .replace(/\n\n/g, '</p><p class="md-p">')
    .replace(/\n/g, '<br/>')
    .replace(/^(?!<)/, '<p class="md-p">')
    .replace(/(?<!>)$/, '</p>')
    // <table>/<ul>/<h1-3> are block-level elements that browsers refuse to
    // nest inside <p>, which silently mangles the DOM (auto-closing the <p>
    // and re-opening a new, often-empty one) and shows up as a big visual
    // gap. Unwrap any paragraph tags that ended up wrapping these blocks.
    .replace(/<p class="md-p">(\s|<br\/>)*(<table|<ul|<h[123])/g, '$2')
    .replace(/(<\/table>|<\/ul>|<\/h[123]>)(\s|<br\/>)*<\/p>/g, '$1')
    // A stray <br/> can also end up directly in front of a block element
    // even when it's not at the very start of the paragraph (e.g. "Here's
    // the data:<br/><table>...") — drop it, since the block element forces
    // its own line break anyway.
    .replace(/(?:<br\/>)+(?=<table|<ul|<h[123])/g, '')
    // Clean up any empty paragraphs left behind by the above.
    .replace(/<p class="md-p">(\s|<br\/>)*<\/p>/g, '');
}

// ─── Inline chart extraction from markdown tables ─────────────────────────────
/**
 * Parses markdown tables embedded in LLM text into ChartSpec objects for inline rendering.
 * Returns an array of { spec, startIndex, endIndex } so the caller can split the text
 * around them and render text + charts interleaved.
 */
function extractInlineCharts(text: string): { spec: ChartSpec; raw: string }[] {
  const results: { spec: ChartSpec; raw: string }[] = [];

  // Match markdown tables: header row | sep row | data rows
  const tableRe = /(\|.+\|\n\|[-| :]+\|\n(?:\|.+\|\n?)+)/g;
  let m: RegExpExecArray | null;
  while ((m = tableRe.exec(text)) !== null) {
    const raw = m[1].trim();
    const lines = raw.split('\n').filter(l => l.trim());
    if (lines.length < 3) continue;

    // Parse header
    const headerCells = lines[0].split('|').map(c => c.trim()).filter(Boolean);
    // Skip separator line (lines[1])
    const dataLines = lines.slice(2);

    if (headerCells.length < 2) continue;

    // Check if 2nd+ columns are numeric
    const dataRows = dataLines.map(l =>
      l.split('|').map(c => c.trim()).filter(Boolean)
    ).filter(r => r.length >= 2);

    if (dataRows.length < 2) continue;

    const isNumeric = (s: string) => !isNaN(parseFloat(s.replace(/[₹,%\s]/g, '').replace(/,/g, '')));
    const parseNum  = (s: string) => parseFloat(s.replace(/[₹,%\s]/g, '').replace(/,/g, '')) || 0;

    // Check that at least one data column is numeric
    const hasNumericCol = dataRows.some(r => r.slice(1).some(isNumeric));
    if (!hasNumericCol) continue;

    // Determine chart type: if first col looks like years/dates → line, else bar
    const firstColVals = dataRows.map(r => r[0]);
    const looksLikeTimeSeries = firstColVals.every(v =>
      /^\d{4}$/.test(v) ||           // Year: 2020
      /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(v) || // Month
      /^q[1-4]/i.test(v) ||          // Quarter
      /^\d{1,2}[\/\-]\d{2,4}/.test(v) // Date
    );

    // Build series for each numeric column beyond the first
    const numericColIndexes = headerCells
      .slice(1)
      .map((_, i) => i + 1)
      .filter(i => dataRows.some(r => r[i] && isNumeric(r[i])));

    if (numericColIndexes.length === 0) continue;

    const series: ChartSeries[] = numericColIndexes.map(colIdx => ({
      name: headerCells[colIdx] ?? `Series ${colIdx}`,
      data: dataRows
        .filter(r => r[colIdx] && isNumeric(r[colIdx]))
        .map(r => ({ label: r[0], value: parseNum(r[colIdx]) })),
    }));

    // Derive a title from surrounding context (look for text before the table)
    const beforeTable = text.slice(0, m.index);
    const titleMatch = beforeTable.match(/(?:^|\n)(?:#{1,3}\s+|[*_]{0,2})([^\n]{5,80})(?:[*_]{0,2})\s*\n?$/);
    const title = titleMatch
      ? titleMatch[1].replace(/[*_#]/g, '').trim()
      : `${headerCells[0]} vs ${headerCells.slice(1).join(', ')}`;

    // Detect % unit
    const unit = headerCells.slice(1).some(h => h.includes('%')) ||
      dataRows.some(r => r.slice(1).some(v => v.includes('%'))) ? '%' : undefined;

    const spec: ChartSpec = {
      type: looksLikeTimeSeries ? 'line' : 'bar',
      title,
      series,
      ...(unit ? { unit } : {}),
    };

    if (isValidChart(spec)) {
      results.push({ spec, raw });
    }
  }
  return results;
}

/**
 * Splits markdown text around embedded tables that were converted to charts,
 * returning an array of { type: 'text' | 'chart', content }.
 */
function splitTextAndCharts(text: string): Array<{ type: 'text'; content: string } | { type: 'chart'; spec: ChartSpec }> {
  const charts = extractInlineCharts(text);
  if (charts.length === 0) return [{ type: 'text', content: text }];

  const parts: Array<{ type: 'text'; content: string } | { type: 'chart'; spec: ChartSpec }> = [];
  let remaining = text;

  for (const { spec, raw } of charts) {
    const idx = remaining.indexOf(raw);
    if (idx === -1) continue;
    const before = remaining.slice(0, idx);
    if (before.trim()) parts.push({ type: 'text', content: before });
    parts.push({ type: 'chart', spec });
    remaining = remaining.slice(idx + raw.length);
  }
  if (remaining.trim()) parts.push({ type: 'text', content: remaining });
  return parts;
}

// ─── Chart validation ────────────────────────────────────────────────────────
function isValidChart(spec: ChartSpec): boolean {
  if (spec.type === 'table') {
    const cols = spec.columns ?? [];
    const rows = spec.rows ?? [];
    return cols.length >= 2 && rows.length >= 1;
  }
  const allPts = (spec.series ?? []).flatMap(s => s.data ?? []);
  if (allPts.length < 2) return false;                          // need at least 2 points
  const vals = allPts.map(d => d.value);
  if (new Set(vals).size < 2) return false;                     // all values identical = useless
  if (vals.every(v => v === 0)) return false;                   // all zero
  const labels = allPts.map(d => d.label);
  if (new Set(labels).size < 2) return false;                   // all same label (e.g. "Today","Today")
  return true;
}

// ─── Mini SVG Charts ──────────────────────────────────────────────────────────
function BarChart({ spec }: { spec: ChartSpec }) {
  const data = (spec.series ?? [])[0]?.data ?? [];
  if (data.length < 2) return null;
  const absVals = data.map(d => Math.abs(d.value));
  const max = Math.max(...absVals, 1);
  const W = 320, H = 150, pad = 32;
  const barW = Math.max(8, Math.floor((W - pad * 2) / data.length - 6));
  const COLORS = ['#1a1f4e','#3b82f6','#22c55e','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#ec4899'];
  // Y-axis grid lines
  const gridVals = [0.25, 0.5, 0.75, 1.0];
  // With many bars (e.g. 10+ monthly points), narrow bars can't fit a value
  // label or full category label without text overlapping its neighbors.
  // Thin those out rather than letting them collide.
  const showValueLabel = barW >= 22;
  const labelStep = data.length > 8 ? Math.ceil(data.length / 6) : 1;
  const maxLabelChars = barW >= 28 ? 9 : barW >= 18 ? 5 : 3;
  return (
    <div className="chart-wrap">
      <div className="chart-title">{spec.title}</div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ overflow: 'visible' }}>
        {gridVals.map(f => (
          <line key={f} x1={pad} y1={H-22-(f*(H-44))} x2={W-pad} y2={H-22-(f*(H-44))}
            stroke="#f0f2f8" strokeWidth="1"/>
        ))}
        <line x1={pad} y1={H-22} x2={W-pad} y2={H-22} stroke="#e2e6f0" strokeWidth="1"/>
        {data.map((d, i) => {
          const x = pad + i * ((W-pad*2)/data.length) + ((W-pad*2)/data.length - barW)/2;
          const bH = Math.max(2,(Math.abs(d.value)/max)*(H-44));
          const isNeg = d.value < 0;
          const color = isNeg ? '#ef4444' : COLORS[i % COLORS.length];
          return (
            <g key={i}>
              <rect x={x} y={isNeg ? H-22 : H-22-bH} width={barW} height={bH} rx="3" fill={color} opacity=".88"/>
              {i % labelStep === 0 && (
                <text x={x+barW/2} y={H-6} textAnchor="middle" fontSize="8" fill="#8b93b5" fontFamily="DM Sans,sans-serif">
                  {d.label.length > maxLabelChars ? d.label.slice(0,maxLabelChars)+ '…' : d.label}
                </text>
              )}
              {showValueLabel && (
                <text x={x+barW/2} y={isNeg ? H-22+bH+10 : H-22-bH-4} textAnchor="middle" fontSize="8" fill={color} fontFamily="DM Mono,monospace" fontWeight="700">
                  {spec.unit==='%' ? `${d.value>0?'+':''}${d.value.toFixed(2)}%` : d.value.toLocaleString('en-IN')}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function LineChart({ spec }: { spec: ChartSpec }) {
  const W = 320, H = 130, pad = 32;
  const series = spec.series ?? [];
  const allV = series.flatMap(s => s.data.map(d => d.value));
  // Need at least 2 distinct time points to draw a meaningful line
  const allLabels = series.flatMap(s => s.data.map(d => d.label));
  const uniqueLabels = new Set(allLabels);
  if (allV.length < 2 || uniqueLabels.size < 2) return null;
  let mn = Math.min(...allV), mx = Math.max(...allV);
  // add 10% padding so line isn't glued to top/bottom edges
  const range = mx - mn || Math.abs(mx) * 0.1 || 1;
  mn -= range * 0.1; mx += range * 0.1;
  const COLORS = ['#1a1f4e','#3b82f6','#22c55e','#f59e0b'];
  function pts(data: ChartDataPoint[]) {
    return data.map((d,i) => {
      const x = pad + (i/Math.max(data.length-1,1))*(W-pad*2);
      const y = (H-28) - ((d.value-mn)/(mx-mn))*(H-48);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
  }
  const labels = series[0]?.data ?? [];
  const step = Math.max(1, Math.ceil(labels.length/5));
  return (
    <div className="chart-wrap">
      <div className="chart-title">{spec.title}</div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`}>
        {[0.25,0.5,0.75,1.0].map(f => (
          <line key={f} x1={pad} y1={(H-28)-(f*(H-48))} x2={W-pad} y2={(H-28)-(f*(H-48))}
            stroke="#f0f2f8" strokeWidth="1"/>
        ))}
        <line x1={pad} y1={H-28} x2={W-pad} y2={H-28} stroke="#e2e6f0" strokeWidth="1"/>
        {series.map((s,si) => (
          <polyline key={si} points={pts(s.data)} fill="none"
            stroke={s.color ?? COLORS[si%COLORS.length]} strokeWidth="2"
            strokeLinecap="round" strokeLinejoin="round"/>
        ))}
        {labels.filter((_,i) => i%step===0 || i===labels.length-1).map((d,_idx) => {
          const oi = labels.indexOf(d);
          const x = pad + (oi/Math.max(labels.length-1,1))*(W-pad*2);
          return <text key={oi} x={x} y={H-12} textAnchor="middle" fontSize="8" fill="#8b93b5" fontFamily="DM Sans,sans-serif">{d.label}</text>;
        })}
      </svg>
      {series.length > 1 && (
        <div className="chart-legend">
          {series.map((s,i) => (
            <span key={i} className="chart-leg">
              <i style={{ background: s.color ?? COLORS[i%COLORS.length] }}/>
              {s.name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function PieChart({ spec }: { spec: ChartSpec }) {
  const data = (spec.series ?? [])[0]?.data ?? [];
  if (data.length < 2) return null;
  const total = data.reduce((s,d) => s+Math.abs(d.value),0) || 1;
  const COLORS = ['#1a1f4e','#3b82f6','#22c55e','#f59e0b','#ef4444','#8b5cf6'];
  let angle = -Math.PI/2;
  const R=52, cx=68, cy=68;
  const slices = data.map((d,i) => {
    const frac = Math.abs(d.value)/total;
    const sweep = frac*2*Math.PI;
    const x1=cx+R*Math.cos(angle), y1=cy+R*Math.sin(angle);
    angle += sweep;
    const x2=cx+R*Math.cos(angle), y2=cy+R*Math.sin(angle);
    return { path:`M${cx} ${cy}L${x1.toFixed(2)} ${y1.toFixed(2)}A${R} ${R} 0 ${sweep>Math.PI?1:0} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}Z`,
      color: COLORS[i%COLORS.length], label: d.label, pct: (frac*100).toFixed(1) };
  });
  return (
    <div className="chart-wrap">
      <div className="chart-title">{spec.title}</div>
      <div style={{ display:'flex', alignItems:'center', gap:12 }}>
        <svg width="136" height="136" viewBox="0 0 136 136">
          {slices.map((s,i) => <path key={i} d={s.path} fill={s.color} stroke="#fff" strokeWidth="1.5" opacity=".9"/>)}
        </svg>
        <div style={{ display:'flex', flexDirection:'column', gap:5 }}>
          {slices.map((s,i) => (
            <div key={i} style={{ display:'flex', alignItems:'center', gap:6, fontSize:11, fontFamily:'DM Sans,sans-serif' }}>
              <span style={{ width:9, height:9, borderRadius:2, background:s.color, flexShrink:0, display:'inline-block' }}/>
              <span style={{ color:'#4b5680' }}>{s.label}</span>
              <span style={{ color:s.color, fontWeight:700, fontFamily:'DM Mono,monospace', marginLeft:'auto', paddingLeft:8 }}>{s.pct}%</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Datawrapper chart embed ──────────────────────────────────────────────────
function DatawrapperChart({ spec }: { spec: ChartSpec }) {
  const dw = spec.datawrapper!;
  const [loaded, setLoaded] = useState(false);
  // Taller for multi-series / more data points
  const nPts = (spec.series ?? []).flatMap(s => s.data ?? []).length;
  const iframeH = spec.type === 'pie' ? 320 : nPts > 8 ? 380 : 300;
  return (
    <div className="chart-wrap dw-chart-wrap">
      <div style={{ position: 'relative', minHeight: iframeH }}>
        {!loaded && (
          <div style={{
            position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center', gap: 8,
            background: 'linear-gradient(135deg,#f8f9fc,#f0f2f8)',
            borderRadius: 10,
          }}>
            <div className="chip-spinner" style={{ width: 18, height: 18, borderWidth: 2.5, borderColor: '#1a1f4e33', borderTopColor: '#1a1f4e' }}/>
            <span style={{ fontSize: 11, color: '#8b93b5', fontFamily: 'DM Sans,sans-serif' }}>Loading chart…</span>
          </div>
        )}
        <iframe
          title={spec.title}
          src={dw.embedUrl}
          style={{ width: '100%', border: 0, height: iframeH, display: 'block', borderRadius: 10, opacity: loaded ? 1 : 0, transition: 'opacity .3s' }}
          scrolling="no"
          allowFullScreen
          onLoad={() => setLoaded(true)}
        />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 4 }}>
        <span style={{ fontSize: 10, color: '#8b93b5', fontFamily: 'DM Sans,sans-serif' }}>{spec.title}</span>
        <a href={dw.publicUrl} target="_blank" rel="noopener noreferrer"
          style={{ fontSize: 10, color: '#3b82f6', fontFamily: 'DM Sans,sans-serif', textDecoration: 'none' }}>
          Open ↗
        </a>
      </div>
    </div>
  );
}

// ─── Table fallback (used only if Datawrapper isn't configured/available) ────
function TableChart({ spec }: { spec: ChartSpec }) {
  const columns = spec.columns ?? [];
  const rows = spec.rows ?? [];
  if (columns.length < 2 || rows.length < 1) return null;
  return (
    <div className="chart-wrap">
      <div className="chart-title">{spec.title}</div>
      <div style={{ overflowX: 'auto' }}>
        <table className="md-table">
          <thead>
            <tr>{columns.map((c,i) => <th key={i} className="md-th">{c}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((r,ri) => (
              <tr key={ri}>{r.map((cell,ci) => <td key={ci} className="md-td">{cell}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ChartBlock({ spec }: { spec: ChartSpec }) {
  // Hard gate: never render a chart that has bad/useless data
  if (!isValidChart(spec)) return null;
  // Datawrapper embed iframes are blocked on the free/starter plan (returns 403
  // for the public CDN URL). Use the inline SVG renderers for the UI — the
  // Datawrapper integration is still used for the PDF export (PNG via API token).
  // Show an "Open in Datawrapper" link if we have the publicUrl.
  const dwLink = spec.datawrapper?.publicUrl;
  const inner = spec.type === 'table' ? <TableChart spec={spec}/>
              : spec.type === 'pie'   ? <PieChart spec={spec}/>
              : spec.type === 'line'  ? <LineChart spec={spec}/>
              : <BarChart spec={spec}/>;
  if (!dwLink) return inner;
  return (
    <div>
      {inner}
      <div style={{ textAlign: 'right', marginTop: 4 }}>
        <a href={dwLink} target="_blank" rel="noopener noreferrer"
          style={{ fontSize: 10, color: '#3b82f6', fontFamily: 'DM Sans,sans-serif', textDecoration: 'none' }}>
          Open in Datawrapper ↗
        </a>
      </div>
    </div>
  );
}

// ─── Report Panel ─────────────────────────────────────────────────────────────
// ─── Email Modal ──────────────────────────────────────────────────────────────
function EmailModal({ onClose, onSend, sending, result, defaultSubject }: {
  onClose: () => void;
  onSend: (subject: string, recipients: string, file: File | null) => void;
  sending: boolean;
  result: { ok: boolean; msg: string } | null;
  defaultSubject: string;
}) {
  const [subject, setSubject]       = useState(defaultSubject);
  const [recipients, setRecipients] = useState('');
  const [csvFile, setCsvFile]       = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const hasRecipients = recipients.includes('@') || csvFile !== null;

  const fld = (label: string, icon: string, child: React.ReactNode) => (
    <div>
      <label style={{ fontSize:11, fontWeight:600, color:'#4b5680', display:'block', marginBottom:5, textTransform:'uppercase', letterSpacing:'.04em' }}>
        {icon} {label}
      </label>
      {child}
    </div>
  );

  const inputStyle: React.CSSProperties = {
    width:'100%', boxSizing:'border-box', padding:'9px 12px', borderRadius:9,
    border:'1.5px solid #e2e6f0', background:'#f8f9fc',
    fontSize:13, color:'#1a1f4e', fontFamily:"'DM Sans',sans-serif",
    outline:'none',
  };

  return (
    <div style={{ position:'fixed', inset:0, zIndex:9999, background:'rgba(15,20,50,.55)', backdropFilter:'blur(4px)', display:'flex', alignItems:'center', justifyContent:'center', padding:20 }}
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{ background:'#fff', borderRadius:16, width:'100%', maxWidth:430, boxShadow:'0 20px 60px rgba(26,31,78,.25)', fontFamily:"'DM Sans',sans-serif", overflow:'hidden' }}>

        {/* Header */}
        <div style={{ background:'#1a1f4e', padding:'16px 20px', display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div style={{ display:'flex', alignItems:'center', gap:10 }}>
            <div style={{ width:32, height:32, borderRadius:9, background:'rgba(255,255,255,.12)', display:'flex', alignItems:'center', justifyContent:'center' }}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#c8860a" strokeWidth="2.2" strokeLinecap="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
            </div>
            <div>
              <div style={{ color:'#fff', fontWeight:700, fontSize:14 }}>Email Report</div>
              <div style={{ color:'rgba(255,255,255,.5)', fontSize:11 }}>Send via Growth Gradual SMTP</div>
            </div>
          </div>
          <button onClick={onClose} style={{ background:'rgba(255,255,255,.1)', border:'none', borderRadius:8, width:28, height:28, color:'rgba(255,255,255,.7)', cursor:'pointer', display:'flex', alignItems:'center', justifyContent:'center' }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>

        {/* Body */}
        <div style={{ padding:'20px 20px 16px', display:'flex', flexDirection:'column', gap:14 }}>

          {fld('Subject', '📝', (
            <input type="text" value={subject} onChange={e => setSubject(e.target.value)}
              disabled={sending} style={inputStyle} placeholder="Growth Gradual Research Report" />
          ))}

          {fld('Recipients (emails, comma-separated)', '📬', (
            <textarea value={recipients} onChange={e => setRecipients(e.target.value)}
              disabled={sending} rows={3} placeholder="alice@example.com, bob@example.com"
              style={{ ...inputStyle, resize:'vertical', lineHeight:1.5 }} />
          ))}

          {/* CSV / Excel upload */}
          <div>
            <label style={{ fontSize:11, fontWeight:600, color:'#4b5680', display:'block', marginBottom:5, textTransform:'uppercase', letterSpacing:'.04em' }}>
              📎 Or upload a CSV / Excel with an &quot;email&quot; column
            </label>
            <div style={{ display:'flex', alignItems:'center', gap:8 }}>
              <button type="button" onClick={() => fileRef.current?.click()} disabled={sending}
                style={{ padding:'8px 14px', borderRadius:9, border:'1.5px solid #e2e6f0', background:'#f8f9fc', color:'#1a1f4e', fontSize:12, cursor:'pointer', fontFamily:"'DM Sans',sans-serif", fontWeight:600 }}>
                {csvFile ? '📄 Change file' : '📂 Choose file'}
              </button>
              {csvFile && (
                <span style={{ fontSize:12, color:'#15803d', fontWeight:600 }}>
                  ✓ {csvFile.name}
                  <button onClick={() => setCsvFile(null)} style={{ marginLeft:6, background:'none', border:'none', color:'#ef4444', cursor:'pointer', fontSize:13 }}>✕</button>
                </span>
              )}
              {!csvFile && <span style={{ fontSize:11, color:'#8b93b5' }}>.csv or .xlsx</span>}
            </div>
            <input ref={fileRef} type="file" accept=".csv,.xlsx" style={{ display:'none' }}
              onChange={e => { const f = e.target.files?.[0]; if (f) setCsvFile(f); e.target.value = ''; }} />
          </div>

          {/* Result */}
          {result && (
            <div style={{ padding:'10px 13px', borderRadius:9, fontSize:12, lineHeight:1.5,
              background: result.ok ? '#f0fdf4' : '#fef2f2',
              border: `1px solid ${result.ok ? '#bbf7d0' : '#fecaca'}`,
              color: result.ok ? '#15803d' : '#dc2626',
              display:'flex', alignItems:'flex-start', gap:8 }}>
              <span style={{ fontSize:16 }}>{result.ok ? '✅' : '❌'}</span>
              <span>{result.msg}</span>
            </div>
          )}

          {/* Actions */}
          <div style={{ display:'flex', gap:8, marginTop:2 }}>
            <button onClick={onClose} style={{ flex:1, padding:'10px', borderRadius:9, border:'1.5px solid #e2e6f0', background:'#f8f9fc', color:'#4b5680', fontSize:13, cursor:'pointer', fontWeight:600, fontFamily:"'DM Sans',sans-serif" }}>
              Cancel
            </button>
            <button onClick={() => onSend(subject, recipients, csvFile)} disabled={!hasRecipients || sending}
              style={{ flex:2, padding:'10px', borderRadius:9, border:'none',
                background: !hasRecipients || sending ? '#8b93b5' : '#1a1f4e',
                color:'#fff', fontSize:13, cursor: !hasRecipients || sending ? 'not-allowed' : 'pointer',
                fontWeight:700, display:'flex', alignItems:'center', justifyContent:'center', gap:7, fontFamily:"'DM Sans',sans-serif" }}>
              {sending
                ? <><span className="dots"><i/><i/><i/></span>Sending…</>
                : <><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M22 2L11 13M22 2L15 22l-4-9-9-4 20-7z"/></svg>Send Report</>}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Report Panel ─────────────────────────────────────────────────────────────
function ReportPanel({ msg, question, hasPriorContext, onGenerate }: { msg: Message; question: string; hasPriorContext: boolean; onGenerate: (includeContext: boolean) => void }) {
  const [open, setOpen]               = useState(false);
  const [pdfLoading, setPdfLoading]   = useState(false);
  const [emailOpen, setEmailOpen]     = useState(false);
  const [emailSending, setEmailSending] = useState(false);
  const [emailResult, setEmailResult] = useState<{ ok: boolean; msg: string } | null>(null);
  // Default ON to match the previous always-include behaviour; only ever
  // shown when there's actually prior conversation to include (i.e. not the
  // very first response in the thread).
  const [includeContext, setIncludeContext] = useState(true);

  const rd = msg.reportData;
  const loading = msg.reportLoading ?? false;
  const done = !!rd;
  const eligible = msg.reportEligible ?? false;

  if (!loading && !done && !eligible) return null;

  const downloadPdf = async () => {
    if (!rd || pdfLoading) return;
    setPdfLoading(true);
    try {
      const res = await fetch('/api/chat/report/pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ report: rd.report, title: rd.title, charts: rd.charts, images: rd.images ?? [], question, keyStats: rd.keyStats, summary: rd.summary, fileImages: rd.fileImages ?? [] }),
      });
      const contentType = res.headers.get('Content-Type') ?? '';
      if (contentType.includes('application/pdf')) {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `growth-gradual-report-${new Date().toISOString().slice(0,10)}.pdf`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      } else {
        const win = window.open('', '_blank');
        if (win) { win.document.write(await res.text()); win.document.close(); win.focus(); setTimeout(() => win.print(), 600); }
      }
    } catch(e) { console.error('[downloadPdf]', e); }
    finally { setPdfLoading(false); }
  };

  /** Strip markdown symbols + collapse all whitespace/newlines into single spaces */
  const sanitizeText = (raw: string) =>
    raw
      .replace(/[*_#`>~[\]]/g, '')   // remove markdown punctuation
      .replace(/\s+/g, ' ')           // newlines → space, collapse runs
      .trim();

  const sendEmail = async (subject: string, recipients: string, file: File | null) => {
    if (!rd) return;
    setEmailSending(true);
    setEmailResult(null);
    try {
      const fd = new FormData();
      fd.append('subject',    subject || 'Growth Gradual Research Report');
      fd.append('recipients', recipients);
      fd.append('report',     rd.report   ?? '');
      // Prefer the clean backend-generated title; fall back to the first H1/H2
      // heading in the report markdown, then the original question.
      const reportTitle = (() => {
        if (rd.title) return sanitizeText(rd.title).slice(0, 120);
        const match = (rd.report ?? '').match(/^#{1,2}\s+(.+)$/m);
        return match ? sanitizeText(match[1]).slice(0, 120) : sanitizeText(question).slice(0, 120);
      })();
      fd.append('title',      reportTitle);
      fd.append('summary',    rd.summary  ?? '');
      fd.append('keyStats',   JSON.stringify(rd.keyStats ?? []));
      if (file) fd.append('file', file);

      const res  = await fetch('/api/chat/report/email', { method: 'POST', body: fd });
      const data = await res.json();
      if (data.success) {
        setEmailResult({ ok: true, msg: data.message ?? 'Report sent!' });
        setTimeout(() => setEmailOpen(false), 2500);
      } else {
        setEmailResult({ ok: false, msg: data.error ?? 'Failed to send.' });
      }
    } catch {
      setEmailResult({ ok: false, msg: 'Network error — could not reach server.' });
    } finally {
      setEmailSending(false);
    }
  };

  // Use the first H1/H2 from the report as the subject, fall back to user question
  const reportHeading = (() => {
    const match = (rd?.report ?? '').match(/^#{1,2}\s+(.+)$/m);
    return match ? sanitizeText(match[1]) : sanitizeText(question);
  })();
  const defaultSubject = `Growth Gradual Research Report — ${reportHeading.slice(0, 60)}${reportHeading.length > 60 ? '…' : ''}`;

  return (
    <>
      {emailOpen && (
        <EmailModal
          onClose={() => { setEmailOpen(false); setEmailResult(null); }}
          onSend={sendEmail}
          sending={emailSending}
          result={emailResult}
          defaultSubject={defaultSubject}
        />
      )}
      <div className="report-wrap">
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          {loading && (
            <div style={{ display:'flex', alignItems:'center', gap:6, fontSize:11, color:'#8b93b5', fontFamily:"'DM Sans',sans-serif" }}>
              <span className="dots"><i/><i/><i/></span>
              Generating report…
            </div>
          )}
          {!loading && !done && eligible && hasPriorContext && (
            <label className="report-context-toggle" title="When on, earlier questions and answers in this chat are included as background for the report">
              <input
                type="checkbox"
                checked={includeContext}
                onChange={(e) => setIncludeContext(e.target.checked)}
              />
              <span className="report-context-track"><span className="report-context-thumb"/></span>
              <span className="report-context-label">Include previous context</span>
            </label>
          )}
          {!loading && !done && eligible && (
            <button className="report-btn" onClick={() => onGenerate(hasPriorContext ? includeContext : false)}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>
              Generate Report
            </button>
          )}
          {done && (
            <>
              <button className="report-btn" onClick={() => setOpen(o => !o)}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                {open ? 'Hide report' : 'Show report'}
              </button>
              <button className="report-btn" onClick={downloadPdf} disabled={pdfLoading}
                style={{ background: pdfLoading ? '#166534' : '#15803d', opacity: pdfLoading ? 0.8 : 1 }}>
                {pdfLoading
                  ? <><span className="dots" style={{marginRight:4}}><i/><i/><i/></span>Building PDF…</>
                  : <><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>Download PDF</>
                }
              </button>
              <button className="report-btn" onClick={() => { setEmailResult(null); setEmailOpen(true); }}
                style={{ background:'#6d28d9' }}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
                Email Report
              </button>
            </>
          )}
        </div>
        {open && done && (
          <div className="report-body">
            <div className="report-content">
              {rd.keyStats.length > 0 && (
                <div className="key-stats-row">
                  {rd.keyStats.map((s,i) => (
                    <div key={i} className="key-stat-card">
                      <div className="key-stat-label">{s.label}</div>
                      <div className="key-stat-value">{s.value}</div>
                      {s.change && <div className={`key-stat-change ${s.change.startsWith('+') ? 'pos' : s.change.startsWith('-') ? 'neg' : ''}`}>{s.change}</div>}
                    </div>
                  ))}
                </div>
              )}
              {/* Render report text with charts + web images interleaved at [CHART_n] / [WEB_IMG_n] placeholders */}
              {(rd.charts.length > 0 || (rd.images?.length ?? 0) > 0)
                ? (() => {
                    // Split on both placeholder kinds in one pass — with 2 capture
                    // groups, String.split yields [text, kind, num, text, kind, num, …]
                    const parts = rd.report.split(/\[(CHART|WEB_IMG)_(\d+)\]/gi);
                    const elements: React.ReactNode[] = [];
                    for (let i = 0; i < parts.length; i += 3) {
                      const text = parts[i];
                      if (text && text.trim()) {
                        elements.push(<div key={`t-${i}`} dangerouslySetInnerHTML={{ __html: renderMd(text) }}/>);
                      }
                      const kind = parts[i + 1];
                      const num = parts[i + 2];
                      if (kind && num !== undefined) {
                        const n = parseInt(num, 10) - 1;
                        if (kind.toUpperCase() === 'CHART') {
                          const spec = rd.charts[n];
                          if (spec) elements.push(<div key={`c-${i}`} className="inline-report-chart"><ChartBlock spec={spec}/></div>);
                        } else {
                          const img = rd.images?.[n];
                          if (img) elements.push(
                            <figure key={`img-${i}`} className="inline-report-image">
                              <img src={proxyImg(img.url)} alt={img.caption ?? ''} loading="lazy" />
                              {img.caption ? <figcaption>{img.caption}</figcaption> : null}
                            </figure>
                          );
                        }
                      }
                    }
                    // Fallback: any charts/images the model didn't place a placeholder for
                    rd.charts.forEach((c, ci) => {
                      const placeholderRe = new RegExp(`\\[CHART_${ci + 1}\\]`, 'i');
                      if (!placeholderRe.test(rd.report)) {
                        elements.push(<div key={`fb-c-${ci}`} className="inline-report-chart"><ChartBlock spec={c}/></div>);
                      }
                    });
                    (rd.images ?? []).forEach((img, ii) => {
                      const placeholderRe = new RegExp(`\\[WEB_IMG_${ii + 1}\\]`, 'i');
                      if (!placeholderRe.test(rd.report)) {
                        elements.push(
                          <figure key={`fb-img-${ii}`} className="inline-report-image">
                              <img src={proxyImg(img.url)} alt={img.caption ?? ''} loading="lazy" />
                            {img.caption ? <figcaption>{img.caption}</figcaption> : null}
                          </figure>
                        );
                      }
                    });
                    return <>{elements}</>;
                  })()
                : <div dangerouslySetInnerHTML={{ __html: renderMd(rd.report) }}/>
              }
            </div>
          </div>
        )}
      </div>
    </>
  );
}


// ─── Stream ───────────────────────────────────────────────────────────────────
// ─── File attachment types ─────────────────────────────────────────────────────
interface AttachedFile {
  id: string;
  name: string;
  size: number;
  type: string;
  content: string; // base64 data URL or raw text
  extractedText?: string;
  status: 'attaching' | 'attached' | 'failed';
  error?: string;
}
interface PastedText {
  id: string;
  label: string;
  text: string;
}
const ACCEPTED_TYPES = [
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/msword',
  'text/plain', 'text/csv', 'text/markdown',
  'image/jpeg', 'image/png', 'image/webp', 'image/gif',
];
const MAX_ATTACH = 10;
function fmtSize(b: number) {
  if (b < 1024) return `${b}B`;
  if (b < 1048576) return `${(b/1024).toFixed(0)}KB`;
  return `${(b/1048576).toFixed(1)}MB`;
}
function wordCount(s: string) { return s.trim().split(/\s+/).filter(Boolean).length; }
function readAsDataURL(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader(); r.onload = () => resolve(r.result as string);
    r.onerror = () => reject(new Error('Read failed')); r.readAsDataURL(file);
  });
}
function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader(); r.onload = () => resolve(r.result as string);
    r.onerror = () => reject(new Error('Read failed')); r.readAsText(file);
  });
}

async function extractPdfText(file: File): Promise<string> {
  try {
    // Use pdf.js to extract text from PDF pages
    const pdfjsLib = await import('pdfjs-dist');
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjsLib.version}/pdf.worker.min.mjs`;
    const dataUrl = await readAsDataURL(file);
    const base64  = dataUrl.split(',')[1];
    const binary  = atob(base64);
    const bytes   = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const pdf       = await pdfjsLib.getDocument({ data: bytes }).promise;
    const numPages  = Math.min(pdf.numPages, 30);
    const textParts: string[] = [];
    for (let p = 1; p <= numPages; p++) {
      const page    = await pdf.getPage(p);
      const content = await page.getTextContent();
      const pageText = content.items
        .map((item: unknown) => (item as { str?: string }).str ?? '')
        .join(' ');
      if (pageText.trim()) textParts.push(`[Page ${p}]\n${pageText.trim()}`);
    }
    return textParts.join('\n\n');
  } catch (e) {
    console.warn('[processFile] pdf.js extraction failed:', e);
    return '';
  }
}

async function processFile(file: File): Promise<AttachedFile | null> {
  const accepted = ACCEPTED_TYPES.includes(file.type) || file.type.startsWith('image/') || file.type.startsWith('text/');
  if (!accepted) return null;
  const id = Math.random().toString(36).slice(2, 10);
  // Return immediately with 'attaching' status
  const base: AttachedFile = { id, name: file.name, size: file.size, type: file.type, content: '', status: 'attaching' };
  try {
    let content = '', extractedText = '';
    if (file.type.startsWith('text/')) {
      extractedText = await readAsText(file);
      content = extractedText;
    } else if (file.type === 'application/pdf') {
      content       = await readAsDataURL(file);
      extractedText = await extractPdfText(file);
    } else {
      content = await readAsDataURL(file);
    }
    return { ...base, content, extractedText, status: 'attached' };
  } catch (e) {
    return { ...base, status: 'failed', error: (e as Error).message };
  }
}
function buildAttachmentContext(files: AttachedFile[], pasted: PastedText[]): string {
  const parts: string[] = [];
  files.forEach((f, i) => {
    if (f.extractedText) {
      parts.push(`[Attached File ${i+1}: ${f.name}]\n${f.extractedText.slice(0, 8000)}`);
    } else if (f.type.startsWith('image/')) {
      parts.push(`[Attached Image ${i+1}: ${f.name}]`);
    } else {
      parts.push(`[Attached File ${i+1}: ${f.name} (${f.type})]`);
    }
  });
  pasted.forEach((p, i) => {
    parts.push(`[Pasted Context ${i+1}: ${p.label}]\n${p.text.slice(0, 6000)}`);
  });
  if (!parts.length) return '';
  return `\n\n---\n📎 USER-ATTACHED CONTEXT:\n${parts.join('\n\n')}\n---\n\nUse the attached content above when answering.`;
}

interface StreamMeta { type:'meta'; searchPerformed:boolean; resultCount:number; queryType:string; sources:Source[]; }

async function* streamReply(
  messages: { role:string; content:string }[],
  signal: AbortSignal,
  onMeta: (m:StreamMeta) => void,
  fileContext?: string,
  sessionId?: string,
  hasRag?: boolean,
  fileImages?: { name:string; mimeType:string; data:string }[],
): AsyncGenerator<string> {
  let res: Response;
  try {
    res = await fetch('/api/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        messages,
        fileContext: fileContext ?? '',
        sessionId:  sessionId  ?? '',
        hasRag:     hasRag     ?? false,
        fileImages: fileImages ?? [],
      }), signal,
    });
  } catch (err) {
    throw new Error(`Failed to fetch: ${(err as Error).message}`);
  }

  if (!res.ok) {
    const body = await res.text().catch(() => '');
    if (res.status === 429) {
      yield '⚠️ AI providers are rate-limited. Please wait 60 seconds and try again.';
    } else {
      yield `*Error ${res.status}${body ? ': ' + body.slice(0, 100) : ''}*`;
    }
    return;
  }

  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    let value: Uint8Array | undefined, done: boolean;
    try {
      ({ value, done } = await reader.read());
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        throw new Error(`Stream read error: ${(err as Error).message}`);
      }
      return;
    }
    if (done) break;
    buf += dec.decode(value, { stream:true });
    const lines = buf.split('\n');
    buf = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const raw = line.slice(6).trim();
      if (raw === '[DONE]') return;
      try {
        const json = JSON.parse(raw);
        if (json.type === 'meta') { onMeta(json as StreamMeta); continue; }
        const t = json.choices?.[0]?.delta?.content;
        if (t) yield t;
      } catch { /* skip malformed chunk */ }
    }
  }
}

// ─── Suggestions ──────────────────────────────────────────────────────────────
const SUGGESTIONS = [
  { icon:'📈', label:'Nifty outlook today' },
  { icon:'🏦', label:'Top banking stocks' },
  { icon:'💰', label:'Best SIPs right now' },
  { icon:'🔍', label:'Analyse HDFC Bank' },
  { icon:'📰', label:'Latest market news' },
  { icon:'🌐', label:'RBI rate cut impact' },
  { icon:'🚀', label:'Upcoming IPOs' },
  { icon:'📊', label:'Sector performance' },
];

// ─── Main Component ───────────────────────────────────────────────────────────
export default function GrowthGradualChat() {
  // Mobile detection
  const [isMobile, setIsMobile] = useState(false);
  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 768);
    check();
    window.addEventListener('resize', check);
    return () => window.removeEventListener('resize', check);
  }, []);

  // Conversations (sidebar history)
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId]           = useState<string | null>(null);
  const [messages, setMessages]           = useState<Message[]>([]);
  const [sidebarOpen, setSidebarOpen]     = useState(true);

  // Close sidebar by default on mobile
  useEffect(() => {
    if (isMobile) setSidebarOpen(false);
    else setSidebarOpen(true);
  }, [isMobile]);

  // Welcome mode: null = home, 'chat' = ask anything, 'attach' = file mode, 'news' = market news
  const [welcomeMode, setWelcomeMode] = useState<null|'chat'|'attach'|'news'>(null);

  // Chat state
  const [input, setInput]       = useState('');
  const [streaming, setStreaming] = useState(false);
  const [searching, setSearching] = useState(false);
  const [statusMsg, setStatusMsg] = useState('');
  const [dragOver, setDragOver]   = useState(false);

  // File attachment state
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([]);
  const [pastedTexts, setPastedTexts]     = useState<PastedText[]>([]);
  const [attachLoading, setAttachLoading] = useState(false);
  const [ragIndexed, setRagIndexed]       = useState(false);
  const [ragIndexing, setRagIndexing]     = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const bottomRef      = useRef<HTMLDivElement>(null);
  const inputRef       = useRef<HTMLTextAreaElement>(null);
  const abortRef       = useRef<AbortController | null>(null);
  const historyRef     = useRef<{ role:string; content:string }[]>([]);
  const initializedRef = useRef(false); // guard against React Strict Mode double-mount

  // ── File processing helper ─────────────────────────────────────────────────
  const addFiles = useCallback(async (fileList: File[]) => {
    const remaining = MAX_ATTACH - attachedFiles.length;
    if (remaining <= 0) return;
    const toProcess = fileList.slice(0, remaining);
    setAttachLoading(true);

    // Add placeholder chips immediately with 'attaching' status
    const placeholders: AttachedFile[] = toProcess.map(f => ({
      id: Math.random().toString(36).slice(2, 10),
      name: f.name, size: f.size, type: f.type,
      content: '', status: 'attaching' as const,
    }));
    setAttachedFiles(prev => [...prev, ...placeholders]);

    // Process each file (extract text) and update chip as it completes
    const results = await Promise.all(toProcess.map(async (file, idx) => {
      const placeholder = placeholders[idx];
      try {
        const result = await processFile(file);
        setAttachedFiles(prev => prev.map(f =>
          f.id === placeholder.id
            ? (result ? { ...result, id: placeholder.id } : { ...placeholder, status: 'failed' as const, error: 'Unsupported file type' })
            : f
        ));
        return result ? { ...result, id: placeholder.id } : null;
      } catch (e) {
        setAttachedFiles(prev => prev.map(f =>
          f.id === placeholder.id
            ? { ...placeholder, status: 'failed' as const, error: (e as Error).message }
            : f
        ));
        return null;
      }
    }));

    setAttachLoading(false);

    const sessionId = getOrCreateSessionId();
    setRagIndexing(true);

    // ── Strategy: index directly to HF Space immediately (don't wait for gateway) ──
    // The Paperly gateway does OCR + storage (useful) but its RAG index step
    // silently fails ~100% of the time (133s upload → 0 chunks indexed).
    // We index directly using the text already extracted by processFile() — this
    // is instant and reliable. The gateway upload runs in the background for storage.
    try {
      const toIndex = results.filter((f): f is AttachedFile => f !== null && !!f.extractedText);

      if (toIndex.length > 0) {
        const docs = toIndex.map(f => ({
          id: f.id,
          name: f.name,
          text: f.extractedText!,
          source_type: 'file',
          file_type: f.type,
        }));

        // ── Index with one automatic retry (handles HF Space cold starts) ──────
        // HF Spaces sleep after ~15 min idle. Cold start = 60-90s. If the first
        // attempt returns 0 chunks (timeout or cold start), we wait 8s and retry
        // once. This covers the most common reason files are silently ignored.
        const tryIndex = async (): Promise<{ chunks_added?: number; total_chunks?: number; error?: string }> => {
          const r = await fetch('/api/rag/index', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, documents: docs }),
          });
          return r.json().catch(() => ({}));
        };

        console.log('[RAG] Indexing', docs.length, 'doc(s) to HF Space...');
        let d = await tryIndex();

        if ((d.chunks_added ?? 0) === 0 && (d.total_chunks ?? 0) === 0) {
          // First attempt got 0 chunks — HF Space may have been cold. Wait and retry.
          console.warn('[RAG] 0 chunks on first attempt — HF Space cold start? Retrying in 8s...');
          setStatusMsg('File server waking up, retrying…');
          await new Promise(res => setTimeout(res, 8_000));
          d = await tryIndex();
        }

        if ((d.chunks_added ?? 0) > 0 || (d.total_chunks ?? 0) > 0) {
          setRagIndexed(true);
          console.log('[RAG] Indexed', d.chunks_added, 'chunks ✅');
        } else {
          // Both attempts failed — tell the user clearly instead of silent failure
          console.error('[RAG] Index failed after retry:', d);
          setAttachedFiles(prev => prev.map(f => ({
            ...f,
            status: 'failed' as const,
            error: 'File server unavailable — your file was attached but AI may not read it. Try re-uploading.',
          })));
        }
      } else {
        console.warn('[RAG] No extracted text available to index');
      }

      // ── Fire gateway upload in background for Supabase storage / OCR ──
      // We don't await this — it takes 60-180s and we already have RAG working.
      const fd = new FormData();
      fd.append('sessionId', sessionId);
      for (const file of toProcess) fd.append('files', file, file.name);
      fetch('/api/upload', {
        method: 'POST',
        body: fd,
        signal: AbortSignal.timeout(300_000),
      }).then(res => {
        if (res.ok) console.log('[Upload] Paperly gateway storage complete (background)');
        else console.warn('[Upload] Paperly gateway storage failed (non-critical):', res.status);
      }).catch(e => {
        console.warn('[Upload] Paperly gateway storage error (non-critical):', e);
      });

    } catch (e) {
      console.warn('[RAG] Direct index failed:', e);
      setAttachedFiles(prev => prev.map(f => ({
        ...f,
        status: f.status === 'attaching' ? 'failed' as const : f.status,
        error: f.status === 'attaching' ? 'Upload failed — please try again.' : f.error,
      })));
    } finally {
      setRagIndexing(false);
      setStatusMsg('');
    }
  }, [attachedFiles.length]);

  // ── Drag-and-drop ─────────────────────────────────────────────────────────
  const handleDragOver  = (e: React.DragEvent) => { e.preventDefault(); setDragOver(true); };
  const handleDragLeave = (e: React.DragEvent) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false); };
  const handleAreaDrop  = async (e: React.DragEvent) => {
    e.preventDefault(); setDragOver(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length) await addFiles(files);
  };

  // ── Paste handler ─────────────────────────────────────────────────────────
  const handlePaste = useCallback(async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = Array.from(e.clipboardData?.items ?? []);
    const fileItems = items.filter(it => it.kind === 'file');
    if (fileItems.length > 0) {
      e.preventDefault();
      const files = fileItems.map(it => it.getAsFile()).filter((f): f is File => f !== null);
      if (files.length) { await addFiles(files); return; }
    }
    // Large text → context chip
    const pasted = e.clipboardData?.getData('text') ?? '';
    if (pasted.trim().length > 120) {
      e.preventDefault();
      const snippet: PastedText = {
        id: Math.random().toString(36).slice(2, 10),
        label: `Text ${pastedTexts.length + 1}`,
        text: pasted.trim(),
      };
      setPastedTexts(prev => [...prev, snippet]);
    }
  }, [addFiles, pastedTexts.length]);

  // Load conversations from localStorage on mount — guarded against Strict Mode double-invoke
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;
    const saved = loadConversations();
    setConversations(saved);
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior:'smooth' });
  }, [messages]);

  // Save whenever conversations change — deduplicate by id before persisting
  useEffect(() => {
    if (!conversations.length) return;
    const seen = new Set<string>();
    const deduped = conversations.filter(c => {
      if (seen.has(c.id)) return false;
      seen.add(c.id);
      return true;
    });
    saveConversations(deduped);
    // If duplicates were found, fix state too
    if (deduped.length !== conversations.length) {
      setConversations(deduped);
    }
  }, [conversations]);

  // New chat
  const startNewChat = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setActiveId(null);
    historyRef.current = [];
    setInput('');
    setAttachedFiles([]);
    setPastedTexts([]);
    setRagIndexed(false);
    setRagIndexing(false);
    setWelcomeMode(null);
    setTimeout(() => inputRef.current?.focus(), 100);
  }, []);

  // Load a conversation
  const loadConversation = useCallback((conv: Conversation) => {
    abortRef.current?.abort();
    // Clear any stale reportLoading flags — a persisted loading state has no active
    // fetch behind it, so the spinner would show forever if not cleared here.
    const cleanedMessages = conv.messages.map(m =>
      m.reportLoading ? { ...m, reportLoading: false } : m
    );
    setMessages(cleanedMessages);
    setActiveId(conv.id);
    historyRef.current = conv.messages.map(m => ({ role: m.role, content: m.text }));
    setTimeout(() => bottomRef.current?.scrollIntoView({ behavior:'smooth' }), 50);
  }, []);

  // Delete a conversation
  const deleteConversation = useCallback((id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setConversations(prev => prev.filter(c => c.id !== id));
    if (activeId === id) startNewChat();
  }, [activeId, startNewChat]);

  // Send message
  const send = useCallback(async (text: string) => {
    const q = text.trim();
    if (!q || streaming) return;
    setInput('');
    if (inputRef.current) { inputRef.current.style.height = 'auto'; }

    const userMsg: Message = { id:uid(), role:'user',      text:q,  ts:Date.now() };
    const botMsg:  Message = { id:uid(), role:'assistant', text:'', ts:Date.now() };
    const newMessages = [...messages, userMsg, botMsg];

    const hasAttachments = attachedFiles.some(f => f.status === 'attached') || pastedTexts.length > 0;

    setMessages(newMessages);
    setStreaming(true);
    setSearching(true);
    setStatusMsg(
      ragIndexed ? 'Reading your documents…'
      : hasAttachments ? 'Analysing your attachment…'
      : 'Searching the web for latest data…'
    );
    // When attachments are present, don't carry over prior conversation history.
    // Sending old turns alongside a new file/image causes the model to answer from
    // previous topic context instead of the attachment. Reset to just the current message.
    const previousHistory = [...historyRef.current];
    if (hasAttachments) {
      historyRef.current = [{ role: 'user', content: q }];
    } else {
      historyRef.current = [...historyRef.current, { role: 'user', content: q }];
    }

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let metaDone = false;
    let finalText = '';
    const fileCtx = buildAttachmentContext(attachedFiles, pastedTexts);

    // Build image payloads for vision-capable main chat (mirrors buildFilePayload logic)
    const chatFileImages: { name:string; mimeType:string; data:string }[] = [];
    for (const f of attachedFiles) {
      if (f.status === 'attached' && f.type.startsWith('image/') && f.content) {
        const base64 = f.content.split(',')[1];
        if (base64) chatFileImages.push({ name: f.name, mimeType: f.type, data: base64 });
      }
    }

    try {
      let acc = '';
      for await (const chunk of streamReply(historyRef.current, ctrl.signal, (meta) => {
        metaDone = true;
        setSearching(false);
        setStatusMsg(meta.searchPerformed ? `Web search · ${meta.sources?.length ?? 0} sources found — generating…` : 'Analysing…');
        // Source count + search metadata — logs only, never rendered in UI
        if (meta.searchPerformed) {
          console.log(
            `[Search] Web search · ${meta.sources?.length ?? 0} sources · type=${meta.queryType}`,
            meta.sources?.map((s: { url?: string }) => s.url) ?? [],
          );
        }
        // Store metadata on message for future reference — intentionally NOT rendered in JSX
        setMessages(prev => prev.map(m => m.id === botMsg.id
          ? { ...m, searchPerformed: meta.searchPerformed, sources: meta.sources, queryType: meta.queryType } : m));
      }, fileCtx, getOrCreateSessionId(), ragIndexed, chatFileImages)) {
        if (!metaDone) { setSearching(false); metaDone = true; }
        acc += chunk;
        if (acc.length > 0) setStatusMsg('');
        finalText = acc;
        setMessages(prev => prev.map(m => m.id === botMsg.id ? { ...m, text:acc } : m));
      }
      // After reply, always restore the full history including this new exchange
      // so follow-up messages within the same session continue to work correctly.
      if (hasAttachments) {
        // Rebuild: old turns + new user msg + new assistant reply
        historyRef.current = [
          ...previousHistory,
          { role: 'user', content: q },
          { role: 'assistant', content: finalText },
        ];
      } else {
        historyRef.current = [...historyRef.current, { role: 'assistant', content: finalText }];
      }

      // Report generation is now manual — the user taps "Generate Report" when
      // they want one. Here we just tag the message as eligible (skipping
      // greetings/chitchat) and stash the question + attachments so the
      // button can build the request later without re-deriving state.
      const botMsgId = botMsg.id;
      const currentFiles = attachedFiles;
      const wantsVisual = /\b(chart|graph|plot|visuali[sz]e|trend\s*line)\b/i.test(q);
      const hasReportableFiles = currentFiles.some(f => f.status === 'attached');
      const isSubstantiveQuery = (() => {
        // Any attached file is inherently substantive — short prompts like
        // "summarize this" or "analyze this file" are exactly when users
        // want a report, so never gate eligibility on message length/chitchat
        // when a file is attached.
        if (hasReportableFiles) return true;
        const lower = q.toLowerCase().trim();
        // Skip very short messages (under 15 chars) — greetings, "hi", "hello", "ok", etc.
        if (q.trim().length < 15) return false;
        // Skip pure greetings / chitchat
        const chitchat = /^(hi|hello|hey|thanks|thank you|ok|okay|sure|great|good|yes|no|bye|test|ping|what'?s up|how are you)/i;
        if (chitchat.test(lower) && q.trim().length < 40) return false;
        return true;
      })();

      setMessages(prev => prev.map(m => m.id === botMsgId
        ? { ...m, reportEligible: isSubstantiveQuery, reportQuestion: q, reportFiles: currentFiles, wantsVisual }
        : m));

      // ── Inline chart generation ────────────────────────────────────────────
      // After any data-heavy finance reply, silently call /api/chat/charts to
      // extract + publish charts from the reply text. Charts are attached to
      // the message and rendered inline above the text.
      //
      // IMPORTANT: if the reply already contains a markdown table that
      // splitTextAndCharts() will turn into an inline chart *inside* the text,
      // skip this call entirely — otherwise the same data gets charted twice
      // (once inline in the text, once again in the "📊 Charts" section above
      // it), which is what produces the duplicated/overlapping chart look.
      const alreadyHasInlineChart = wantsVisual && splitTextAndCharts(finalText).some(p => p.type === 'chart');
      const chartLengthOk = wantsVisual ? finalText.length > 80 : finalText.length > 200;
      if (isSubstantiveQuery && chartLengthOk && !alreadyHasInlineChart) {
        (async () => {
          try {
            const chartRes = await fetch('/api/chat/charts', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ question: q, reply: finalText, wantsVisual }),
            });
            if (!chartRes.ok) return;
            const chartData = await chartRes.json();
            const charts: ChartSpec[] = (chartData.charts ?? []).filter((c: ChartSpec) => {
              const pts = (c.series ?? []).flatMap(s => s.data ?? []);
              return pts.length >= 2 && new Set(pts.map(p => p.value)).size >= 2 && new Set(pts.map(p => p.label)).size >= 2;
            });
            if (charts.length > 0) {
              setMessages(prev => prev.map(m => m.id === botMsgId ? { ...m, inlineCharts: charts } : m));
            }
          } catch { /* silent — charts are non-critical */ }
        })();
      }
      // ── End inline chart generation ────────────────────────────────────────

      // ── Follow-up question generation ──────────────────────────────────────
      // Always show follow-up chips after every bot reply.
      // For greetings/chitchat: show hardcoded starter suggestions instantly.
      // For substantive queries: generate contextual ones via API.
      const GREETING_FOLLOWUPS = [
        { icon: '📈', label: 'Nifty outlook today' },
        { icon: '🏦', label: 'Top banking stocks' },
        { icon: '💰', label: 'Best SIPs right now' },
        { icon: '🚀', label: 'Upcoming IPOs' },
        { icon: '📊', label: 'Sector performance' },
      ];

      if (!isSubstantiveQuery) {
        // Greeting/chitchat — show starter chips immediately, no API call
        setMessages(prev => prev.map(m => m.id === botMsgId
          ? { ...m, followUpQuestions: GREETING_FOLLOWUPS.map(s => `${s.icon} ${s.label}`) }
          : m));
      } else {
        // Substantive query — generate contextual follow-ups via API
        (async () => {
          try {
            const fuRes = await fetch('/api/chat', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                messages: [
                  {
                    role: 'user',
                    content:
                      `Based on this Q&A about Indian finance/markets, suggest exactly 3 short follow-up questions ` +
                      `a user might ask next. Add a relevant emoji before each question. ` +
                      `Return ONLY a JSON array of 3 strings, no explanation, no markdown.\n\n` +
                      `Q: ${q.slice(0, 200)}\nA: ${finalText.slice(0, 600)}`,
                  },
                ],
                fileContext: '',
                sessionId: '',
                hasRag: false,
                fileImages: [],
                _followUpMode: true,
              }),
            });
            if (!fuRes.ok) return;
            const reader = fuRes.body!.getReader();
            const dec = new TextDecoder();
            let buf = '', acc = '';
            while (true) {
              const { value, done } = await reader.read();
              if (done) break;
              buf += dec.decode(value, { stream: true });
              const lines = buf.split('\n');
              buf = lines.pop() ?? '';
              for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const raw = line.slice(6).trim();
                if (raw === '[DONE]') break;
                try {
                  const j = JSON.parse(raw);
                  const t = j.choices?.[0]?.delta?.content;
                  if (t) acc += t;
                } catch { /* ignore */ }
              }
            }
            const match = acc.match(/\[[\s\S]*\]/);
            if (!match) return;
            const questions: string[] = JSON.parse(match[0]);
            if (!Array.isArray(questions) || questions.length === 0) return;
            setMessages(prev => prev.map(m => m.id === botMsgId
              ? { ...m, followUpQuestions: questions.slice(0, 3).map(s => String(s).trim()).filter(Boolean) }
              : m));
          } catch { /* silent fail — follow-ups are non-critical */ }
        })();
      }
      // ── End follow-up generation ───────────────────────────────────────────

      // Persist conversation
      const title = q.length > 46 ? q.slice(0,46)+'…' : q;
      setMessages(prev => {
        const final = prev;
        setConversations(convPrev => {
          if (activeId) {
            return convPrev.map(c => c.id === activeId ? { ...c, messages:final, ts:Date.now() } : c);
          } else {
            // Guard: don't add if a conv with this title was just created (Strict Mode double-fire)
            const alreadyExists = convPrev.some(c => c.title === title && Date.now() - c.ts < 2000);
            if (alreadyExists) return convPrev;
            const newConv: Conversation = { id:uid(), title, messages:final, ts:Date.now() };
            setActiveId(newConv.id);
            return [newConv, ...convPrev];
          }
        });
        return final;
      });
    } catch(e:unknown) {
      setSearching(false);
      setStatusMsg('');
      if ((e as Error)?.name === 'AbortError') return;
      const errMsg = (e as Error)?.message || '';
      const isNetwork = errMsg.includes('fetch') || errMsg.includes('network') || errMsg.includes('Failed');
      setMessages(prev => prev.map(m => m.id === botMsg.id ? {
        ...m,
        text: isNetwork
          ? '*Network error — please check your connection and try again.*'
          : '*Something went wrong. Please try again.*',
      } : m));
    } finally {
      setStreaming(false);
      setSearching(false);
      setStatusMsg('');
    }
  }, [streaming, messages, activeId, attachedFiles, pastedTexts]);

  /** Builds the report request for one message's attachments and fires it.
   *  Triggered on demand by the "Generate Report" button — reports are no
   *  longer generated automatically after every reply.
   *  `conversationContext` is a summary of all prior Q&A in the thread. */
  const generateReport = useCallback((botMsgId: string, question: string, files: AttachedFile[], conversationContext?: string) => {
    setMessages(prev => prev.map(m => m.id === botMsgId ? { ...m, reportLoading: true } : m));

    const buildFilePayload = async () => {
      const fileImages: { name: string; mimeType: string; data: string }[] = [];
      const fileTextParts: string[] = [];

      for (const f of files) {
        if (f.extractedText) {
          fileTextParts.push(`[File: ${f.name}]\n${f.extractedText.slice(0, 12000)}`);
        }

        if (f.type === 'application/pdf') {
          // Render PDF pages to images using pdf.js
          try {
            const pdfjsLib = await import('pdfjs-dist');
            pdfjsLib.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjsLib.version}/pdf.worker.min.mjs`;
            const dataUrl = f.content; // base64 data URL
            const base64 = dataUrl.split(',')[1];
            const binary = atob(base64);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            const pdf = await pdfjsLib.getDocument({ data: bytes }).promise;
            const numPages = Math.min(pdf.numPages, 8); // max 8 pages
            for (let pageNum = 1; pageNum <= numPages; pageNum++) {
              const page = await pdf.getPage(pageNum);
              const viewport = page.getViewport({ scale: 1.5 });
              const canvas = document.createElement('canvas');
              canvas.width = viewport.width;
              canvas.height = viewport.height;
              const ctx = canvas.getContext('2d')!;
              await page.render({ canvasContext: ctx, viewport }).promise;
              const imgData = canvas.toDataURL('image/jpeg', 0.85).split(',')[1];
              fileImages.push({ name: `${f.name} page ${pageNum}`, mimeType: 'image/jpeg', data: imgData });
            }
          } catch (e) {
            console.warn('[report] pdf.js render failed:', e);
          }
        } else if (f.type.startsWith('image/')) {
          // Direct image attachment
          const base64 = f.content.split(',')[1];
          if (base64) fileImages.push({ name: f.name, mimeType: f.type, data: base64 });
        }
      }

      return { fileImages, fileTextContext: fileTextParts.join('\n\n') };
    };

    // Source-document concepts for the OKF bundle — one per attached file,
    // independent of how much text made it into the LLM prompt (capped above
    // at 12000 chars per file for the prompt; the OKF concept can carry more
    // since it's just markdown, capped again server-side at 4000 chars).
    const sourceDocuments = files
      .filter(f => f.extractedText)
      .map(f => ({ name: f.name, text: f.extractedText || '', file_type: f.type }));

    buildFilePayload().then(({ fileImages, fileTextContext }) => {
      return fetch('/api/chat/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question,
          sources:     [],
          // Actual uploaded file text ONLY — conversationContext used to be
          // merged in here too, but the backend treats fileContext as
          // "PRIMARY SOURCE, highest priority" for data extraction, which
          // caused prior conversation topics to leak into the new report's
          // title/content. It's sent as its own field below instead.
          fileContext: fileTextContext,
          conversationContext: conversationContext || '',
          fileImages,
          sessionId:   getOrCreateSessionId(),
          hasRag:      ragIndexed,
        }),
      });
    })
      .then(r => r.json())
      .then(data => {
        setMessages(prev => prev.map(m => m.id === botMsgId ? {
          ...m,
          reportLoading: false,
          reportData: {
            report:     data.report     ?? '',
            title:      data.title      ?? '',
            charts:     data.charts     ?? [],
            images:     data.images     ?? [],
            keyStats:   data.keyStats   ?? [],
            summary:    data.summary    ?? '',
            fileImages: data.fileImages ?? [],
            sourceDocuments,
          },
        } : m));
      })
      .catch(() => {
        setMessages(prev => prev.map(m => m.id === botMsgId ? { ...m, reportLoading: false } : m));
      });
  }, [ragIndexed]);

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); send(input); }
  };

  const isEmpty = messages.length === 0;

  // Group conversations by date
  const grouped = conversations.reduce((acc, c) => {
    const d = fmtDate(c.ts);
    if (!acc[d]) acc[d] = [];
    acc[d].push(c);
    return acc;
  }, {} as Record<string, Conversation[]>);

  return (
    <>
<style>{`
        /* ════════════════════════════════════════════════════════════════════
           GROWTH GRADUAL CHAT — FULLY FLUID RESPONSIVE STYLES
           All dimensions use clamp(), %, svh/svw, or fluid calc() so the
           UI fits every screen from 320 px phone to 4K desktop.
        ════════════════════════════════════════════════════════════════════ */

        /* ── Shell ──────────────────────────────────────────────────────── */
        .chat-shell {
          display: flex;
          /* Navbar (~58px) + ticker (~30px) + page padding (~22px) = ~110px */
          height: calc(100svh - 112px);
          min-height: 0;
          background: #ffffff;
          border-radius: clamp(6px, 1vw, 14px);
          border: 1px solid #e0e5ef;
          box-shadow: 0 2px 20px rgba(15,23,42,.08);
          overflow: hidden;
          font-family: 'DM Sans', sans-serif;
          position: relative;
        }

        /* ── Sidebar ────────────────────────────────────────────────────── */
        .sidebar {
          width: clamp(200px, 22vw, 280px);
          flex-shrink: 0;
          background: #f0f2f7;
          border-right: 1px solid #e2e6f0;
          display: flex;
          flex-direction: column;
          transition: width .25s cubic-bezier(.4,0,.2,1), transform .25s cubic-bezier(.4,0,.2,1);
          overflow: hidden;
        }
        .sidebar--closed { width: 0 !important; }

        .sidebar-hdr {
          padding: clamp(10px,1.5vh,14px) clamp(10px,1.5vw,16px);
          display: flex; align-items: center; gap: 10px;
          border-bottom: 1px solid rgba(26,31,78,.1);
          flex-shrink: 0; background: #fff;
        }
        .sidebar-logo {
          width: clamp(28px,3.5vw,36px); height: clamp(28px,3.5vw,36px);
          border-radius: 9px; background: #ffffff;
          display: flex; align-items: center; justify-content: center;
          flex-shrink: 0; overflow: hidden;
          border: 1px solid #e2e6f0; box-shadow: 0 1px 4px rgba(26,31,78,.08);
        }
        .sidebar-brand {
          color: #0f172a; font-size: clamp(11px,1.1vw,13px);
          font-weight: 800; font-family: 'Playfair Display',serif;
          line-height: 1.2; white-space: nowrap; overflow: hidden;
        }
        .sidebar-brand span {
          display: block; font-size: clamp(7px,.8vw,9px); font-weight: 500;
          color: #94a3b8; font-family: 'DM Sans',sans-serif;
          letter-spacing: .1em; text-transform: uppercase; margin-top: 1px;
        }

        .new-chat-btn {
          margin: clamp(8px,1vh,12px) clamp(8px,1vw,14px);
          display: flex; align-items: center; gap: 8px;
          padding: clamp(7px,1vh,10px) clamp(10px,1.2vw,14px);
          border-radius: 9px; border: 1.5px solid #1a1f4e;
          background: #1a1f4e; color: #fff;
          font-size: clamp(11px,1vw,13px); font-family: 'DM Sans',sans-serif;
          cursor: pointer; transition: background .15s, box-shadow .15s, transform .15s;
          flex-shrink: 0; white-space: nowrap;
          box-shadow: 0 2px 8px rgba(26,31,78,.22); font-weight: 600;
        }
        .new-chat-btn:hover { background: #252b68; box-shadow: 0 4px 14px rgba(26,31,78,.3); transform: translateY(-1px); }

        .conv-list {
          flex: 1; overflow-y: auto; padding: 4px 0 12px;
          scrollbar-width: thin; scrollbar-color: #d0d5e8 transparent;
        }
        .conv-list::-webkit-scrollbar { width: 3px; }
        .conv-list::-webkit-scrollbar-thumb { background: #d0d5e8; border-radius: 4px; }

        .conv-group-label {
          padding: 10px 14px 4px; font-size: 9px; color: #b0b8d4;
          text-transform: uppercase; letter-spacing: .1em;
          font-family: 'DM Sans',sans-serif; white-space: nowrap;
        }
        .conv-item {
          display: flex; align-items: center; gap: 8px;
          padding: clamp(6px,.8vh,9px) 12px;
          cursor: pointer; border-radius: 8px;
          margin: 1px 6px; transition: background .13s; position: relative;
        }
        .conv-item:hover { background: rgba(26,31,78,.07); }
        .conv-item--active { background: rgba(26,31,78,.1); border-left: 2.5px solid #1a6b5a; padding-left: 9.5px; }
        .conv-item__icon { flex-shrink: 0; opacity: .45; }
        .conv-item__title {
          flex: 1; font-size: clamp(11px,1vw,12.5px); color: #4b5680;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
          font-family: 'DM Sans',sans-serif;
        }
        .conv-item--active .conv-item__title { color: #0d4f3c; font-weight: 700; }
        .conv-del {
          opacity: 0; width: 22px; height: 22px; border-radius: 6px;
          border: none; background: rgba(26,31,78,.07); color: #8b93b5;
          cursor: pointer; display: flex; align-items: center; justify-content: center;
          flex-shrink: 0; transition: opacity .15s, background .15s;
        }
        .conv-item:hover .conv-del { opacity: 1; }
        .conv-del:hover { background: rgba(239,68,68,.12); color: #ef4444; }

        .sidebar-footer {
          padding: 10px 14px 14px; border-top: 1px solid rgba(26,31,78,.1);
          font-size: clamp(9px,.9vw,10.5px); color: #8b93b5;
          font-family: 'DM Sans',sans-serif; flex-shrink: 0;
          background: linear-gradient(0deg,#f4f5fa 0%,#f8f9fd 100%);
        }

        /* ── Main chat area ─────────────────────────────────────────────── */
        .chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; background: #f8f9fb; }

        /* ── Top bar ────────────────────────────────────────────────────── */
        .chat-topbar {
          display: flex; align-items: center; gap: clamp(6px,.8vw,12px);
          padding: clamp(8px,1.2vh,12px) clamp(10px,1.5vw,18px);
          background: #0f172a; border-bottom: 1px solid rgba(255,255,255,0.07);
          flex-shrink: 0;
        }
        .topbar-toggle {
          width: clamp(26px,2.8vw,32px); height: clamp(26px,2.8vw,32px);
          border-radius: 8px; border: 1px solid rgba(255,255,255,0.1);
          background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.5);
          cursor: pointer; display: flex; align-items: center; justify-content: center;
          transition: background .15s, color .15s; flex-shrink: 0;
        }
        .topbar-toggle:hover { background: rgba(255,255,255,0.1); color: #fff; }
        .topbar-logo {
          width: clamp(24px,2.5vw,30px); height: clamp(24px,2.5vw,30px);
          border-radius: 8px; overflow: hidden;
          display: flex; align-items: center; justify-content: center;
          flex-shrink: 0; background: transparent;
        }
        .topbar-info { flex: 1; min-width: 0; overflow: hidden; }
        .topbar-name {
          font-size: clamp(12px,1.3vw,14px); font-weight: 700; color: #fff;
          font-family: 'Playfair Display',serif; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .topbar-sub {
          font-size: clamp(9px,.9vw,10.5px); color: rgba(255,255,255,0.4);
          display: flex; align-items: center; gap: 5px;
          white-space: nowrap; overflow: hidden;
        }
        .topbar-dot { width: 6px; height: 6px; border-radius: 50%; background: #22c55e; box-shadow: 0 0 6px rgba(34,197,94,0.7); animation: blink 2s ease-in-out infinite; }
        .topbar-web-badge {
          margin-left: auto; display: flex; align-items: center; gap: 5px;
          padding: 4px clamp(6px,.8vw,12px);
          background: rgba(13,92,69,0.2); border: 1px solid rgba(13,92,69,0.4);
          border-radius: 20px; font-size: clamp(9px,.9vw,11px);
          color: rgba(255,255,255,0.75); font-family: 'DM Sans',sans-serif;
          font-weight: 500; white-space: nowrap; flex-shrink: 0;
        }
        @keyframes blink { 0%,100%{opacity:1;}50%{opacity:.3;} }

        /* ── Messages ───────────────────────────────────────────────────── */
        .chat-msgs {
          flex: 1; overflow-y: auto;
          padding: clamp(12px,2vh,22px) 0 clamp(8px,1vh,14px);
          display: flex; flex-direction: column; gap: 0;
          scrollbar-width: thin; scrollbar-color: #e4e8ef transparent;
          background: #f8f9fb;
        }
        .chat-msgs::-webkit-scrollbar { width: 4px; }
        .chat-msgs::-webkit-scrollbar-thumb { background: #e2e6f0; border-radius: 4px; }

        .msg-row { padding: clamp(3px,.5vh,7px) 0; display: flex; }
        .msg-row--user { justify-content: flex-end; }
        .msg-row--bot  { justify-content: flex-start; }

        .msg-inner {
          display: flex; gap: clamp(6px,.8vw,13px); align-items: flex-start;
          max-width: min(820px, 92%);
          padding: 0 clamp(10px,1.5vw,26px);
          width: 100%;
        }
        .msg-row--user .msg-inner { flex-direction: row-reverse; margin-left: auto; }
        .msg-row--bot  .msg-inner { margin-right: auto; }

        .msg-avatar {
          width: clamp(26px,2.8vw,34px); height: clamp(26px,2.8vw,34px);
          border-radius: 10px; flex-shrink: 0;
          display: flex; align-items: center; justify-content: center;
          margin-top: 2px; overflow: hidden;
        }
        .msg-avatar--bot { background: linear-gradient(135deg,#0d4f3c,#1a1f4e); border: 1px solid rgba(13,79,60,.2); box-shadow: 0 2px 6px rgba(13,79,60,.18); }
        .msg-avatar--user { background: linear-gradient(135deg,#e8eaf5,#d4d8ec); border: 1px solid #c4c9e0; }

        .msg-bubble { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
        .msg-text {
          padding: clamp(9px,1.2vh,13px) clamp(11px,1.4vw,17px);
          border-radius: 18px;
          font-size: clamp(12.5px,1.2vw,14px); line-height: 1.68; word-break: break-word;
        }
        .msg-row--user .msg-text {
          background: linear-gradient(135deg,#1a1f4e,#252b68); color: #fff;
          border-bottom-right-radius: 5px; box-shadow: 0 2px 10px rgba(26,31,78,.2);
        }
        .msg-row--bot .msg-text {
          background: #ffffff; color: #1a1f4e;
          border: 1px solid #e2e6f0; border-bottom-left-radius: 5px;
          box-shadow: 0 1px 4px rgba(26,31,78,.05);
        }
        .msg-ts { font-size: clamp(9px,.85vw,11px); color: #b0b8d4; padding: 0 4px; }
        .msg-row--user .msg-ts { text-align: right; }
        .followup-chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
        .followup-label { font-size:10px; font-weight:600; color:#8892b0; text-transform:uppercase; letter-spacing:.06em; flex-basis:100%; margin-bottom:3px; }
        .followup-chip {
          display:inline-flex; align-items:center; gap:6px;
          background:#fff; border:1.5px solid #e2e6f0;
          border-radius:50px; padding:6px 13px;
          font-size:clamp(11px,1vw,12.5px); color:#1a1f4e;
          cursor:pointer; font-family:inherit; text-align:left; line-height:1.3;
          transition:background .15s, border-color .15s, box-shadow .15s, transform .1s;
          box-shadow:0 1px 3px rgba(26,31,78,.07);
        }
        .followup-chip:hover { background:#f0f3ff; border-color:#c7d0f0; box-shadow:0 2px 8px rgba(26,31,78,.12); transform:translateY(-1px); }
        .followup-chip:active { transform:translateY(0); }
        .followup-chip-icon { font-size:13px; line-height:1; }

        /* Markdown */
        .msg-text .md-p { margin: 0 0 8px; }
        .msg-text .md-p:last-child { margin-bottom: 0; }
        .msg-text .md-h1,.msg-text .md-h2,.msg-text .md-h3 { font-family: 'Playfair Display',serif; color: #1a1f4e; margin: 11px 0 5px; }
        .msg-text .md-h1{font-size:clamp(14px,1.5vw,17px);} .msg-text .md-h2{font-size:clamp(13px,1.3vw,15px);} .msg-text .md-h3{font-size:clamp(12px,1.2vw,13.5px);}
        .msg-text .md-ul { margin: 4px 0 8px 16px; padding: 0; list-style: disc; }
        .msg-text .md-li { margin: 3px 0; }
        .msg-text .md-code { background: rgba(26,31,78,.06); padding: 1px 6px; border-radius: 4px; font-family: 'DM Mono',monospace; font-size: clamp(11px,1vw,12.5px); }
        .msg-text .md-ref { color: #3b82f6; font-size: 10px; }
        .msg-text .md-table { border-collapse: collapse; margin: 8px 0; width: 100%; font-size: clamp(11px,1vw,12.5px); display: block; overflow-x: auto; }
        .msg-text .md-table thead tr { background: #1a1f4e; }
        .msg-text .md-th { background: linear-gradient(90deg,#0d4f3c,#1a1f4e); color: #fff; font-weight: 600; font-size: clamp(10px,.9vw,11px); text-transform: uppercase; letter-spacing: .04em; border: 1px solid #1a3a30; padding: clamp(4px,.6vh,7px) clamp(6px,.8vw,10px); text-align: left; }
        .msg-text .md-td { border: 1px solid #e2e6f0; padding: clamp(4px,.5vh,6px) clamp(6px,.8vw,10px); }
        .msg-text .md-table tbody tr:nth-child(even) td { background: #f8f9fc; }

        /* Status indicators */
        .searching {
          display: flex; align-items: center; gap: 7px;
          padding: 10px 14px; background: rgba(13,79,60,.07);
          border: 1px solid rgba(13,79,60,.18); border-radius: 12px;
          font-size: clamp(11px,1vw,12.5px); color: #0d4f3c; border-bottom-left-radius: 4px;
        }
        .searching svg { animation: spin 1.2s linear infinite; }
        @keyframes spin { to{transform:rotate(360deg);} }
        .search-dots { display: flex; gap: 3px; }
        .search-dots span { width: 4px; height: 4px; border-radius: 50%; background: #0d4f3c; animation: bd .9s ease-in-out infinite; }
        .search-dots span:nth-child(2){animation-delay:.15s;} .search-dots span:nth-child(3){animation-delay:.3s;}
        @keyframes bd{0%,60%,100%{transform:translateY(0);}30%{transform:translateY(-5px);}}

        .typing-wrap { display: flex; align-items: center; gap: 12px; padding: 6px 0; }
        .typing-dots { display: flex; gap: 4px; padding: 12px 16px; background: #fff; border: 1px solid #e2e6f0; border-radius: 18px; border-bottom-left-radius: 5px; box-shadow: 0 1px 4px rgba(26,31,78,.05); }
        .typing-dot { width: 6px; height: 6px; border-radius: 50%; background: #1a1f4e; animation: bd .9s ease-in-out infinite; }
        .typing-dot:nth-child(2){animation-delay:.15s;} .typing-dot:nth-child(3){animation-delay:.3s;}

        .web-search-chip {
          display: inline-flex; align-items: center; gap: 4px; margin-top: 7px; padding: 3px 8px;
          background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 20px;
          font-size: 11px; color: #1d4ed8; font-family: 'DM Sans',sans-serif; font-weight: 500;
        }

        /* Report */
        .report-wrap { margin-top: 7px; }
        .inline-chart-wrap { margin: 10px 0; background: #f8f9fc; border-radius: 10px; border: 1px solid #e2e6f0; padding: 4px 6px 6px; overflow: hidden; }
        .inline-chart-wrap .chart-wrap { margin: 0; background: transparent; border: none; box-shadow: none; padding: 4px 0 0; }
        .inline-report-chart { margin: 18px 0; }
        .inline-report-image { margin: 18px 0; text-align: center; }
        .inline-report-image img { max-width: 100%; max-height: 360px; border-radius: 10px; border: 1px solid #e2e6f0; box-shadow: 0 1px 4px rgba(26,31,78,.06); }
        .inline-report-image figcaption { margin-top: 6px; font-size: 11px; color: #8b93b5; font-style: italic; font-family: 'DM Sans',sans-serif; }
        .inline-charts-section { margin-bottom: 10px; border: 1px solid #e2e6f0; border-radius: 12px; overflow: hidden; background: #f8f9fc; }
        .inline-charts-label { padding: 7px 12px 4px; font-size: 10px; font-weight: 700; color: #8b93b5; text-transform: uppercase; letter-spacing: .06em; font-family: 'DM Sans',sans-serif; }
        .inline-charts-section .charts-grid { padding: 0 6px 8px; gap: 8px; }
        .inline-charts-section .chart-wrap { background: #fff; box-shadow: 0 1px 4px rgba(26,31,78,.06); }
        .dw-chart-wrap { padding: 6px 6px 4px !important; }
        .report-btn { display:flex;align-items:center;gap:5px;background:linear-gradient(135deg,#0d4f3c,#1a1f4e);border:none;border-radius:7px;padding:6px 12px;font-size:clamp(10px,.9vw,11.5px);color:#fff;cursor:pointer;font-family:'DM Sans',sans-serif;transition:opacity .15s,box-shadow .15s;box-shadow:0 2px 8px rgba(13,79,60,.25); }
        .report-btn:hover { opacity: .85; }
        .report-context-toggle { position:relative;display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;font-family:'DM Sans',sans-serif; }
        .report-context-toggle input { position:absolute;opacity:0;width:0;height:0; }
        .report-context-track { position:relative;width:28px;height:16px;border-radius:999px;background:#d7dbea;transition:background .15s;flex-shrink:0; }
        .report-context-thumb { position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:transform .15s; }
        .report-context-toggle input:checked + .report-context-track { background:#0d4f3c; }
        .report-context-toggle input:checked + .report-context-track .report-context-thumb { transform:translateX(12px); }
        .report-context-toggle input:focus-visible + .report-context-track { outline:2px solid #1a1f4e;outline-offset:2px; }
        .report-context-label { font-size:clamp(10px,.85vw,11px);color:#5a6178; }
        .report-body { margin-top:8px;background:#f8f9fc;border:1px solid #e2e6f0;border-radius:12px;padding:clamp(10px,2vw,18px);max-height:clamp(280px,40vh,560px);overflow-y:auto; }
        .report-loading { display:flex;align-items:center;gap:8px;font-size:12px;color:#8b93b5;font-family:'DM Sans',sans-serif; }
        .dots { display:inline-flex;gap:4px; }
        .dots i { width:6px;height:6px;border-radius:50%;background:#8b93b5;animation:bd .9s ease-in-out infinite;display:block; }
        .dots i:nth-child(2){animation-delay:.15s;} .dots i:nth-child(3){animation-delay:.3s;}
        .report-content { font-size:clamp(12px,1.1vw,13.5px);line-height:1.65;color:#1a1f4e;font-family:'DM Sans',sans-serif; }
        .report-content .md-p{margin:0 0 10px;}
        .report-content .md-h1,.report-content .md-h2,.report-content .md-h3{font-family:'Playfair Display',serif;color:#1a1f4e;margin:14px 0 6px;}
        .report-content .md-h1{font-size:clamp(15px,1.6vw,18px);} .report-content .md-h2{font-size:clamp(13px,1.3vw,15px);} .report-content .md-h3{font-size:clamp(12px,1.1vw,13px);}
        .report-content .md-ul{margin:4px 0 10px 16px;list-style:disc;}
        .report-content .md-li{margin:3px 0;}
        .report-content .md-table{border-collapse:collapse;margin:10px 0;width:100%;font-size:clamp(11px,.95vw,12.5px);display:block;overflow-x:auto;}
        .report-content .md-th{background:linear-gradient(90deg,#0d4f3c,#1a1f4e);color:#fff;font-weight:600;font-size:clamp(10px,.85vw,11px);text-transform:uppercase;letter-spacing:.04em;border:1px solid #1a3a30;padding:clamp(5px,.7vh,8px) clamp(7px,.9vw,11px);text-align:left;}
        .report-content .md-td{border:1px solid #e2e6f0;padding:clamp(5px,.6vh,7px) clamp(7px,.9vw,11px);}
        .report-content .md-table tbody tr:nth-child(even) td{background:#f8f9fc;}

        /* Charts */
        .charts-grid { display:grid;grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr));gap:clamp(8px,1.2vw,16px);margin-bottom:clamp(10px,1.5vh,20px); }
        .key-stats-row { display:flex;gap:clamp(6px,.8vw,12px);flex-wrap:wrap;margin-bottom:clamp(10px,1.5vh,18px); }
        .key-stat-card { background:#fff;border:1px solid #e2e6f0;border-radius:10px;padding:clamp(8px,1.2vh,14px) clamp(10px,1.4vw,18px);min-width:80px;flex:1; }
        .key-stat-label { font-size:clamp(9px,.85vw,10.5px);text-transform:uppercase;letter-spacing:.07em;color:#8b93b5;margin-bottom:4px; }
        .key-stat-value { font-size:clamp(14px,1.5vw,18px);font-weight:700;color:#1a1f4e;line-height:1; }
        .key-stat-change { font-size:clamp(10px,.9vw,11.5px);margin-top:4px;font-weight:600; }
        .key-stat-change.pos { color:#16a34a; } .key-stat-change.neg { color:#dc2626; }
        .chart-wrap { background:#fff;border:1px solid #e2e6f0;border-radius:10px;padding:clamp(10px,1.3vw,16px) clamp(10px,1.3vw,16px) 8px; }
        .chart-title { font-size:clamp(10px,.9vw,11.5px);font-weight:600;color:#1a1f4e;font-family:'DM Sans',sans-serif;margin-bottom:8px; }
        .chart-legend { display:flex;flex-wrap:wrap;gap:8px;margin-top:6px; }
        .chart-leg { display:flex;align-items:center;gap:4px;font-size:10px;color:#4b5680;font-family:'DM Sans',sans-serif; }
        .chart-leg i { width:8px;height:8px;border-radius:2px;display:block; }

        /* ── Welcome screen ─────────────────────────────────────────────── */
        .chat-welcome {
          flex: 1; display: flex; flex-direction: column;
          align-items: center; justify-content: center;
          gap: clamp(12px,2vh,22px);
          padding: clamp(16px,3vw,28px) clamp(14px,3vw,28px) clamp(10px,1.5vh,16px);
          text-align: center; background: #f8f9fb;
          overflow-y: auto;
        }
        .welcome-glow { display: none; }
        .welcome-title {
          font-size: clamp(18px,3vw,26px); font-weight: 800;
          background: linear-gradient(135deg,#0d5c45 20%,#1a1f4e 80%);
          -webkit-background-clip: text; -webkit-text-fill-color: transparent;
          font-family: 'Playfair Display',serif; margin: 0; letter-spacing: -.3px;
        }
        .welcome-sub {
          font-size: clamp(11.5px,1.2vw,13px); color: #64748b;
          line-height: 1.6; margin: 2px 0 0; max-width: min(440px, 90%);
        }
        .sugs { display:flex;flex-wrap:wrap;gap:clamp(4px,.5vw,8px);justify-content:center;max-width:min(680px,96%); }
        .sug {
          display:flex;align-items:center;gap:5px;
          padding:clamp(5px,.7vh,8px) clamp(8px,1vw,13px);
          border-radius:20px;border:1.5px solid #d8e4de;
          background:#fff;cursor:pointer;
          font-size:clamp(10.5px,1vw,12px);color:#1a2035;
          transition:all .15s;font-family:'DM Sans',sans-serif;
          box-shadow:0 1px 3px rgba(13,92,69,0.05);
        }
        .sug:hover { border-color:#0d5c45;background:#f0f8f4;transform:translateY(-1px);box-shadow:0 3px 8px rgba(13,92,69,0.1); }
        .sug-icon { font-size:clamp(11px,1.1vw,14px);line-height:1;font-family:'Apple Color Emoji','Segoe UI Emoji','Noto Color Emoji',sans-serif; }

        /* ── Input area ─────────────────────────────────────────────────── */
        .chat-input-area {
          padding: clamp(8px,1.2vh,14px) clamp(10px,2vw,26px) clamp(10px,1.5vh,20px);
          background: #ffffff; border-top: 1px solid rgba(26,31,78,.09);
          flex-shrink: 0; display: flex; flex-direction: column; gap: clamp(5px,.7vh,9px);
          box-shadow: 0 -2px 12px rgba(26,31,78,.04);
        }
        .input-row {
          display: flex; align-items: flex-end; gap: clamp(6px,.8vw,11px);
          background: #f8faf9; border: 1.5px solid #ccddd6; border-radius: 16px;
          padding: clamp(8px,1.1vh,12px) clamp(8px,1vw,12px) clamp(8px,1.1vh,12px) clamp(12px,1.5vw,20px);
          transition: border-color .15s, box-shadow .15s;
          max-width: min(820px, 100%); margin: 0 auto; width: 100%;
        }
        .input-row:focus-within { border-color: #0d4f3c; box-shadow: 0 0 0 3px rgba(13,79,60,.07); }
        .chat-textarea {
          flex: 1; background: none; border: none; outline: none;
          color: #1a1f4e; font-size: clamp(13px,1.2vw,15px); font-family: 'DM Sans',sans-serif;
          line-height: 1.5; resize: none;
          max-height: clamp(80px,15vh,140px); min-height: 22px;
          scrollbar-width: none;
        }
        .chat-textarea::placeholder { color: #b0b8d4; }
        .chat-textarea::-webkit-scrollbar { display: none; }
        .send-btn {
          width: clamp(34px,3.5vw,40px); height: clamp(34px,3.5vw,40px);
          border-radius: 11px; border: none;
          background: linear-gradient(135deg,#0d4f3c,#0f5c47); color: #fff; cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          flex-shrink: 0; transition: opacity .15s, transform .15s, box-shadow .15s;
          box-shadow: 0 3px 10px rgba(13,79,60,.35);
        }
        .send-btn:disabled{opacity:.22;cursor:not-allowed;box-shadow:none;}
        .send-btn:not(:disabled):hover{transform:scale(1.08);box-shadow:0 5px 16px rgba(13,79,60,.45);}
        .send-btn:not(:disabled):active{transform:scale(.93);}
        .input-hint { font-size: clamp(9px,.85vw,10.5px); color: #b0b8d4; text-align: center; font-family: 'DM Sans',sans-serif; }

        /* ── File chips ─────────────────────────────────────────────────── */
        .chips-row { display:flex;flex-wrap:wrap;gap:6px;padding:clamp(6px,.8vh,12px) clamp(10px,1.5vw,20px) 4px;max-height:90px;overflow-y:auto; }
        .file-chip { display:flex;align-items:center;gap:5px;padding:4px 6px 4px 9px;border-radius:20px;background:#f0f3ff;border:1px solid #c8d0e8;font-family:'DM Sans',sans-serif;flex-shrink:0;max-width:min(220px,42vw); }
        .file-chip--text { background:#f0fdf4;border-color:rgba(34,197,94,0.35); }
        .file-chip--img  { background:#f0fdf4;border-color:rgba(34,197,94,0.35); }
        .chip-dot { width:7px;height:7px;border-radius:50%;flex-shrink:0; }
        .chip-name { font-size:clamp(10.5px,.95vw,12px);color:#1a1f4e;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:clamp(80px,14vw,140px); }
        .chip-size { font-size:10px;color:#8b93b5;font-family:'DM Mono',monospace;flex-shrink:0; }
        .chip-x { width:16px;height:16px;border-radius:50%;border:none;background:transparent;color:#ef4444;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;line-height:1;font-weight:700;flex-shrink:0;opacity:0.55;transition:opacity .15s,background .15s;padding:0; }
        .chip-x:hover { opacity:1;background:rgba(239,68,68,0.12); }
        @keyframes spin { to { transform:rotate(360deg); } }
        .chip-spinner { width:12px;height:12px;border-radius:50%;flex-shrink:0;border:2px solid #c8d0e8;border-top-color:#1a1f4e;animation:spin .7s linear infinite; }

        /* ── Attach / News buttons ──────────────────────────────────────── */
        .attach-btn {
          width:clamp(28px,3vw,34px);height:clamp(28px,3vw,34px);
          border-radius:9px;border:1.5px solid #ccddd6;background:#f0f8f4;
          color:#0d4f3c;cursor:pointer;flex-shrink:0;
          display:flex;align-items:center;justify-content:center;
          transition:background .15s,color .15s,border-color .15s,box-shadow .15s;
        }
        .attach-btn:hover:not(:disabled) { background:#0d4f3c;color:#fff;border-color:#0d4f3c;box-shadow:0 2px 8px rgba(13,79,60,.25); }
        .attach-btn:disabled { opacity:0.35;cursor:not-allowed; }

        .news-btn {
          height:clamp(28px,3vw,34px);padding:0 clamp(8px,1vw,13px);
          border-radius:9px;border:1.5px solid #c7ccdc;
          background:#f4f5f9;color:#1a1f4e;cursor:pointer;flex-shrink:0;
          display:flex;align-items:center;gap:5px;
          font-size:clamp(10px,.9vw,11.5px);font-weight:600;
          font-family:'DM Sans',sans-serif;
          transition:background .15s,color .15s,border-color .15s,box-shadow .15s;
        }
        .news-btn:hover { background:#1a1f4e;color:#fff;border-color:#1a1f4e;box-shadow:0 2px 8px rgba(26,31,78,.2); }
        .news-btn--active { background:#1a1f4e;color:#fff;border-color:#1a1f4e; }

        .chat-shell--drag { outline:2.5px dashed #0d4f3c;outline-offset:-2px; }

        /* ══════════════════════════════════════════════════════════════════
           MOBILE SIDEBAR DRAWER
        ══════════════════════════════════════════════════════════════════ */
        .sidebar--mobile {
          position: fixed !important;
          top: 0; left: 0; bottom: 0; z-index: 50;
          height: 100dvh; width: clamp(240px,78vw,300px) !important;
          box-shadow: 4px 0 30px rgba(15,23,42,0.25);
          border-radius: 0 16px 16px 0;
          transition: transform .25s cubic-bezier(.4,0,.2,1) !important;
        }
        .sidebar--mobile.sidebar--closed {
          width: clamp(240px,78vw,300px) !important;
          transform: translateX(-105%) !important;
        }
        .sidebar--mobile:not(.sidebar--closed) {
          transform: translateX(0) !important;
        }

        /* ══════════════════════════════════════════════════════════════════
           BREAKPOINTS
        ══════════════════════════════════════════════════════════════════ */

        /* ── Small phone (≤ 480px) ──────────────────────────────────────── */
        @media (max-width: 480px) {
          .chat-shell { height: calc(100dvh - 108px); border-radius: 8px; }
          .chat-topbar { padding: 7px 10px; gap: 6px; }
          .topbar-web-badge { display: none; }
          .topbar-name { font-size: 12px; }
          .topbar-sub { font-size: 9px; }
          .msg-inner { padding: 0 10px; max-width: 100%; gap: 0; }
          .msg-avatar--bot { display: none; }
          .msg-row--bot .msg-bubble { width: 100%; }
          .msg-text { font-size: 13px; padding: 9px 12px; }
          .chat-welcome { padding: 18px 12px 10px; gap: 12px; justify-content: flex-start; padding-top: 24px; }
          .welcome-title { font-size: 19px; }
          .welcome-sub { font-size: 11.5px; }
          .sugs { justify-content: flex-start; }
          .sug { font-size: 10.5px; padding: 5px 9px; }
          .chat-input-area { padding: 7px 10px 10px; gap: 5px; }
          .input-row { padding: 7px 7px 7px 12px; border-radius: 14px; }
          .chat-textarea { font-size: 15px; }
          .input-hint { display: none; }
          .chips-row { padding: 6px 10px 3px; }
          .key-stat-card { padding: 8px 10px; }
          .key-stat-value { font-size: 14px; }
          .charts-grid { grid-template-columns: 1fr; }
          .report-body { padding: 10px; }
        }

        /* ── Medium phone (481–767px) ───────────────────────────────────── */
        @media (min-width: 481px) and (max-width: 767px) {
          .chat-shell { height: calc(100dvh - 110px); border-radius: 10px; }
          .chat-topbar { padding: 8px 12px; }
          .topbar-web-badge { display: none; }
          .msg-inner { padding: 0 12px; max-width: 100%; }
          .msg-avatar--bot { display: none; }
          .msg-row--bot .msg-bubble { width: 100%; }
          .msg-text { font-size: 13.5px; }
          .chat-welcome { padding: 20px 16px 12px; gap: 14px; }
          .sugs { justify-content: flex-start; }
          .chat-input-area { padding: 8px 12px 12px; }
          .input-hint { display: none; }
          .charts-grid { grid-template-columns: 1fr; }
        }

        /* ── Tablet portrait (768–1023px) ───────────────────────────────── */
        @media (min-width: 768px) and (max-width: 1023px) {
          .chat-shell { height: calc(100dvh - 112px); }
          .sidebar { width: clamp(180px,24vw,230px); }
          .msg-inner { padding: 0 16px; max-width: min(700px, 94%); }
          .topbar-web-badge { padding: 3px 8px; font-size: 9.5px; }
          .chat-input-area { padding: 10px 16px 14px; }
          .charts-grid { grid-template-columns: repeat(auto-fit,minmax(200px,1fr)); }
        }

        /* ── Tablet landscape / small desktop (1024–1279px) ────────────── */
        @media (min-width: 1024px) and (max-width: 1279px) {
          .chat-shell { height: calc(100dvh - 114px); }
          .sidebar { width: clamp(210px,20vw,260px); }
          .msg-inner { max-width: min(760px, 90%); }
        }

        /* ── Large desktop (≥ 1280px) ───────────────────────────────────── */
        @media (min-width: 1280px) {
          .chat-shell { height: calc(100dvh - 116px); }
          .sidebar { width: clamp(240px,18vw,290px); }
          .msg-inner { max-width: min(860px, 88%); }
          .welcome-title { font-size: 28px; }
          .welcome-sub { font-size: 13.5px; }
        }

        /* ── Ultra-wide (≥ 1920px) ──────────────────────────────────────── */
        @media (min-width: 1920px) {
          .chat-shell { max-width: 1600px; margin: 0 auto; }
          .sidebar { width: 300px; }
          .msg-inner { max-width: 920px; }
          .msg-text { font-size: 15px; }
          .welcome-title { font-size: 32px; }
        }

        /* ── Short screens (height < 600px, landscape phones) ───────────── */
        @media (max-height: 600px) {
          .chat-shell { height: calc(100dvh - 88px); }
          .chat-welcome { padding: 10px 14px 8px; gap: 8px; }
          .welcome-title { font-size: 16px; }
          .welcome-sub { display: none; }
          .sugs { max-height: 80px; overflow-y: auto; }
          .chat-input-area { padding: 6px 12px 8px; }
          .sidebar-footer { display: none; }
        }

        /* ── Tall screens (height > 900px) ─────────────────────────────── */
        @media (min-height: 900px) {
          .chat-msgs { padding-top: clamp(24px,3vh,36px); }
          .chat-welcome { gap: 28px; }
        }
      `}</style>


      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept=".pdf,.doc,.docx,.txt,.csv,.md,.jpg,.jpeg,.png,.webp,.gif"
        style={{ display:'none' }}
        onChange={async e => {
          const files = Array.from(e.target.files ?? []);
          if (files.length) await addFiles(files);
          e.target.value = '';
        }}
      />

      <div
        className={`chat-shell${dragOver ? ' chat-shell--drag' : ''}`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleAreaDrop}
      >
        {dragOver && (
          <div style={{ position:'absolute', inset:0, zIndex:50, background:'rgba(26,31,78,0.07)',
            display:'flex', alignItems:'center', justifyContent:'center',
            backdropFilter:'blur(4px)', borderRadius:16, pointerEvents:'none' }}>
            <div style={{ background:'#fff', border:'2px dashed #1a1f4e', borderRadius:16,
              padding:'32px 48px', textAlign:'center', boxShadow:'0 8px 32px rgba(26,31,78,.15)' }}>
              <div style={{ fontSize:36, marginBottom:10 }}>📎</div>
              <p style={{ fontSize:16, fontWeight:700, color:'#1a1f4e', margin:0 }}>Drop files to attach</p>
              <p style={{ fontSize:12, color:'#8b93b5', margin:'4px 0 0' }}>PDF · DOCX · TXT · Images</p>
            </div>
          </div>
        )}

        {/* ── Sidebar ────────────────────────────────────────────────────── */}
        {/* Mobile sidebar overlay backdrop */}
        {isMobile && sidebarOpen && (
          <div
            onClick={() => setSidebarOpen(false)}
            style={{
              position: 'fixed', inset: 0, zIndex: 49,
              background: 'rgba(15,23,42,0.45)', backdropFilter: 'blur(2px)',
            }}
          />
        )}

        <aside className={`sidebar${sidebarOpen ? '' : ' sidebar--closed'}${isMobile ? ' sidebar--mobile' : ''}`}>
          {/* Sidebar header */}
          <div className="sidebar-hdr">
            <div className="sidebar-logo">
              <Image src="/growth-gradual-icon.png" alt="Growth Gradual" width={32} height={32} style={{ objectFit:'contain' }}/>
            </div>
            <div className="sidebar-brand">
              Growth Gradual
              <span>In The Money</span>
            </div>
            {/* Mobile close button */}
            {isMobile && (
              <button
                onClick={() => setSidebarOpen(false)}
                style={{
                  marginLeft:'auto', width:28, height:28, borderRadius:8,
                  border:'1px solid #e2e6f0', background:'#f5f5f8',
                  color:'#8b93b5', cursor:'pointer', flexShrink:0,
                  display:'flex', alignItems:'center', justifyContent:'center',
                }}
                aria-label="Close sidebar"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                  <path d="M18 6L6 18M6 6l12 12"/>
                </svg>
              </button>
            )}
          </div>

          {/* New chat button */}
          <button className="new-chat-btn" onClick={() => { startNewChat(); if (isMobile) setSidebarOpen(false); }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>
            New chat
          </button>

          {/* Conversation list */}
          <div className="conv-list">
            {conversations.length === 0 ? (
              <div style={{ padding:'16px 14px', fontSize:11, color:'#b0b8d4', fontFamily:'DM Sans,sans-serif', textAlign:'center', lineHeight:1.6 }}>
                No conversations yet.<br/>Start chatting below.
              </div>
            ) : (
              Object.entries(grouped).map(([date, convs]) => (
                <div key={date}>
                  <div className="conv-group-label">{date}</div>
                  {convs.map(conv => (
                    <div
                      key={conv.id}
                      className={`conv-item${activeId === conv.id ? ' conv-item--active' : ''}`}
                      onClick={() => { loadConversation(conv); if (isMobile) setSidebarOpen(false); }}
                    >
                      <svg className="conv-item__icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#8b93b5" strokeWidth="2" strokeLinecap="round">
                        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
                      </svg>
                      <span className="conv-item__title">{conv.title}</span>
                      <button className="conv-del" onClick={(e) => deleteConversation(conv.id, e)} aria-label="Delete">
                        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
                      </button>
                    </div>
                  ))}
                </div>
              ))
            )}
          </div>

          <div className="sidebar-footer">
            Growth Gradual · Indian Markets AI<br/>
            Powered by Groq + Web Search
          </div>
        </aside>

        {/* ── Main area ──────────────────────────────────────────────────── */}
        <div className="chat-main">

          {/* Top bar */}
          <div className="chat-topbar">
            <button className="topbar-toggle" onClick={() => setSidebarOpen(o => !o)} aria-label="Toggle sidebar">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
            </button>
            <div className="topbar-logo">
              <Image src="/growth-gradual-icon.png" alt="Growth Gradual" width={30} height={30} style={{ objectFit:'contain', mixBlendMode:'luminosity' as React.CSSProperties['mixBlendMode'], opacity:0.88 }}/>
            </div>
            <div className="topbar-info">
              <div className="topbar-name">Growth Gradual</div>
              <div className="topbar-sub">
                <span className="topbar-dot"/>
                In The Money · Indian Markets AI
              </div>
            </div>
            <div className="topbar-web-badge">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
              Web search enabled
            </div>

          </div>

          {/* Messages */}
          <div className="chat-msgs">
            {isEmpty ? (
              <div className="chat-welcome">
                {/* Headline */}
                <div>
                  <p className="welcome-title">Where should we begin?</p>
                  <p className="welcome-sub">Your AI analyst for Indian markets — powered by live web search & document analysis.</p>
                </div>

                {/* Quick suggestion chips */}
                <div className="sugs">
                  {SUGGESTIONS.map(s => (
                    <button key={s.label} className="sug" onClick={() => send(s.label)}>
                      <span className="sug-icon">{s.icon}</span>
                      <span>{s.label}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((msg, i) => {
                const isLast = i === messages.length - 1;
                const isUser = msg.role === 'user';

                // Status / loading state (searching, indexing, generating)
                if (isLast && streaming && !isUser && (!msg.text || searching || statusMsg)) return (
                  <div key={msg.id} className="msg-row msg-row--bot">
                    <div className="msg-inner">
                      <div className="msg-avatar msg-avatar--bot">
                        <Image src="/growth-gradual-icon.png" alt="" width={28} height={28} style={{ objectFit:'contain' }}/>
                      </div>
                      <div className="msg-bubble">
                        <div className="searching">
                          {statusMsg?.startsWith('Web search') ? (
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#1d4ed8" strokeWidth="2.5" strokeLinecap="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                          ) : (
                            <div className="chip-spinner" style={{ width:12, height:12, borderWidth:2 }}/>
                          )}
                          {statusMsg || 'Thinking…'}
                          <div className="search-dots"><span/><span/><span/></div>
                        </div>
                      </div>
                    </div>
                  </div>
                );

                return (
                  <div key={msg.id} className={`msg-row msg-row--${isUser ? 'user' : 'bot'}`}>
                    <div className="msg-inner">
                      {/* Avatar */}
                      <div className={`msg-avatar msg-avatar--${isUser ? 'user' : 'bot'}`}>
                        {isUser ? (
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4b5680" strokeWidth="2" strokeLinecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                        ) : (
                          <Image src="/growth-gradual-icon.png" alt="" width={28} height={28} style={{ objectFit:'contain' }}/>
                        )}
                      </div>
                      {/* Bubble */}
                      <div className="msg-bubble">

                        {isUser ? (
                          <div
                            className="msg-text"
                            dangerouslySetInnerHTML={{ __html: msg.text.replace(/\n/g,'<br/>') }}
                          />
                        ) : (() => {
                          // Only swap a markdown table for a rendered chart when the
                          // user's question actually asked for one (chart/graph/plot/
                          // visualize). Otherwise a "give me a table" request would
                          // silently lose its table and show a bar chart instead —
                          // respect what was asked for; the table itself renders fine
                          // via renderMd()'s <table class="md-table"> handling.
                          const parts = msg.wantsVisual ? splitTextAndCharts(msg.text) : [];
                          const hasCharts = parts.some(p => p.type === 'chart');
                          const hasInlineCharts = (msg.inlineCharts ?? []).length > 0;
                          return (
                            <>
                              {/* Datawrapper/SVG charts from /api/chat/charts */}
                              {hasInlineCharts && (
                                <div className="inline-charts-section">
                                  <div className="inline-charts-label">📊 Charts</div>
                                  <div className="charts-grid">
                                    {(msg.inlineCharts ?? []).map((c, ci) => <ChartBlock key={ci} spec={c}/>)}
                                  </div>
                                </div>
                              )}
                              {/* Text with embedded table-extracted charts */}
                              {!hasCharts ? (
                                <div
                                  className="msg-text"
                                  dangerouslySetInnerHTML={{ __html: renderMd(msg.text) }}
                                />
                              ) : (
                                <div className="msg-text">
                                  {parts.map((p, pi) =>
                                    p.type === 'text'
                                      ? <div key={pi} dangerouslySetInnerHTML={{ __html: renderMd(p.content) }}/>
                                      : <div key={pi} className="inline-chart-wrap"><ChartBlock spec={p.spec}/></div>
                                  )}
                                </div>
                              )}
                            </>
                          );
                        })()}

                        {!isUser && (msg.reportEligible || msg.reportLoading || msg.reportData) && (
                          <ReportPanel
                            msg={msg}
                            question={msg.reportQuestion ?? msg.text}
                            hasPriorContext={i > 1}
                            onGenerate={(includeContext) => {
                              // Build conversation context from all prior messages in this thread —
                              // only when the toggle is on (it's hidden/defaults false for the
                              // very first response, since there's nothing prior to include).
                              const conversationContext = includeContext
                                ? messages
                                    .slice(0, i) // all messages before this one
                                    .filter(m => m.text && m.text.trim())
                                    .map(m => `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.text.slice(0, 800)}`)
                                    .join('\n\n')
                                : '';
                              generateReport(
                                msg.id,
                                msg.reportQuestion ?? msg.text,
                                msg.reportFiles ?? [],
                                conversationContext || undefined,
                              );
                            }}
                          />
                        )}
                        {/* Follow-up question chips */}
                        {!isUser && isLast && !streaming && msg.followUpQuestions && msg.followUpQuestions.length > 0 && (
                          <div className="followup-chips">
                            <span className="followup-label">You might also ask</span>
                            {msg.followUpQuestions.map((q, qi) => {
                              // Split leading emoji from text (handles emoji followed by space)
                              const emojiMatch = q.match(/^(\p{Emoji_Presentation}|\p{Extended_Pictographic})\s*/u);
                              const icon = emojiMatch ? emojiMatch[0].trim() : '💬';
                              const label = emojiMatch ? q.slice(emojiMatch[0].length) : q;
                              return (
                                <button key={qi} className="followup-chip" onClick={() => send(label)}>
                                  <span className="followup-chip-icon">{icon}</span>
                                  <span>{label}</span>
                                </button>
                              );
                            })}
                          </div>
                        )}
                        {/* Web search chip hidden — sources used internally only */}
                        <span className="msg-ts">{fmtTime(msg.ts)}</span>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
            <div ref={bottomRef}/>
          </div>

          {/* Input */}
          <div className="chat-input-area">

            {/* File + text chips */}
            {(attachedFiles.length > 0 || pastedTexts.length > 0 || attachLoading) && (
              <div className="chips-row" style={{ position:'relative' }}>
                {attachedFiles.length > 0 && (
                  <div style={{
                    position:'absolute', top:-28, left:'50%', transform:'translateX(-50%)',
                    background:'#1a1f4e', color:'#fff', borderRadius:20,
                    fontSize:11, fontWeight:600, padding:'3px 10px',
                    display:'flex', alignItems:'center', gap:5, whiteSpace:'nowrap',
                    boxShadow:'0 2px 8px rgba(26,31,78,.25)',
                    pointerEvents:'none',
                  }}>
                    📎 {attachedFiles.filter(f => f.status==='attached').length} of {attachedFiles.length} file{attachedFiles.length>1?'s':''} attached
                  </div>
                )}
                {attachedFiles.map(f => (
                  <div key={f.id}
                    className={`file-chip${f.type.startsWith('image/') ? ' file-chip--img' : ''}`}
                    title={f.status === 'failed' ? (f.error || 'Failed to read file') : f.name}
                    style={{
                      borderColor: f.status === 'failed' ? '#fca5a5' : f.status === 'attaching' ? '#c4b5fd' : undefined,
                      background:  f.status === 'failed' ? '#fef2f2' : f.status === 'attaching' ? '#f5f3ff' : undefined,
                    }}
                  >
                    {/* Image thumbnail OR icon dot */}
                    {f.type.startsWith('image/') && f.content && f.status === 'attached'
                      ? <img src={f.content} alt="" style={{ width:22, height:22, borderRadius:5, objectFit:'cover', flexShrink:0 }} />
                      : f.status === 'attaching'
                        ? <div className="chip-spinner"/>
                        : <span className="chip-dot" style={{ background: f.status === 'failed' ? '#ef4444' : f.type === 'application/pdf' ? '#ef4444' : '#3b82f6' }} />
                    }

                    {/* Label: Loading… → ✓ name */}
                    <span className="chip-name">
                      {f.status === 'attaching'
                        ? `Loading ${f.name.length > 12 ? f.name.slice(0,10)+'…' : f.name}`
                        : f.status === 'failed'
                          ? `✗ ${f.name.length > 14 ? f.name.slice(0,12)+'…' : f.name}`
                          : `✓ ${f.name.length > 16 ? f.name.slice(0,14)+'…' : f.name}`
                      }
                    </span>

                    {/* Size — only when attached */}
                    {f.status === 'attached' && (
                      <span className="chip-size">{(f.size/1024).toFixed(0)}K</span>
                    )}

                    {f.status !== 'attaching' && (
                      <button className="chip-x" onClick={() => setAttachedFiles(prev => prev.filter(x => x.id !== f.id))} title={`Remove ${f.name}`}>×</button>
                    )}
                  </div>
                ))}
                {pastedTexts.map(p => (
                  <div key={p.id} className="file-chip file-chip--text" title={p.text.slice(0,80)}>
                    <span style={{ fontSize:11 }}>📋</span>
                    <span className="chip-name">{p.label}</span>
                    <span className="chip-size">{wordCount(p.text)}w</span>
                    <button className="chip-x" onClick={() => setPastedTexts(prev => prev.filter(x => x.id !== p.id))}>×</button>
                  </div>
                ))}
              </div>
            )}

            <div className="input-row">
              {/* Attach button */}
              <button
                className="attach-btn"
                onClick={() => fileInputRef.current?.click()}
                disabled={attachLoading || attachedFiles.length >= MAX_ATTACH}
                title="Attach files — PDF, DOCX, TXT, images"
              >
                {attachLoading
                  ? <div className="chip-spinner" style={{ width:15,height:15 }}/>
                  : <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
                }
              </button>

              <textarea
                ref={inputRef}
                className="chat-textarea"
                placeholder={attachedFiles.length > 0 || pastedTexts.length > 0
                  ? 'Ask anything about your attached files…'
                  : 'Ask about Nifty, stocks, MFs, RBI, IPOs…'}
                value={input}
                rows={1}
                onChange={e => {
                  setInput(e.target.value);
                  e.target.style.height = 'auto';
                  e.target.style.height = Math.min(e.target.scrollHeight, 130) + 'px';
                }}
                onKeyDown={onKey}
                onPaste={handlePaste}
                disabled={streaming || attachedFiles.some(f => f.status === 'attaching') || ragIndexing}
              />

              <button className="send-btn" onClick={() => send(input)} disabled={!input.trim() || streaming || attachedFiles.some(f => f.status === 'attaching') || ragIndexing} aria-label="Send">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M22 2L11 13M22 2L15 22l-4-9-9-4 20-7z"/>
                </svg>
              </button>
            </div>
            <p className="input-hint">
              Growth Gradual · Enter to send · Shift+Enter for newline
              {attachedFiles.length > 0 || pastedTexts.length > 0
                ? ` · ${attachedFiles.length + pastedTexts.length} item${attachedFiles.length + pastedTexts.length !== 1 ? 's' : ''} attached`
                : ''}
            </p>
          </div>

        </div>
      </div>
    </>
  );
}
