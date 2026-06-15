'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import Image from 'next/image';

// ─── Types ────────────────────────────────────────────────────────────────────
interface Source { title: string; url: string; snippet: string; }
interface ChartDataPoint { label: string; value: number; }
interface ChartSeries { name: string; data: ChartDataPoint[]; color?: string; }
interface ChartSpec { type: 'bar' | 'line' | 'pie'; title: string; series: ChartSeries[]; unit?: string; }
interface ReportData { report: string; charts: ChartSpec[]; keyStats: {label:string;value:string;change?:string}[]; summary: string; fileImages?: {name:string;mimeType:string;data:string}[]; }
interface Message {
  id: string; role: 'user' | 'assistant'; text: string; ts: number;
  sources?: Source[]; searchPerformed?: boolean; queryType?: string;
  reportData?: ReportData; reportLoading?: boolean;
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

/** Return a stable UUID for this browser — created once, stored in localStorage. */
function getOrCreateSessionId(): string {
  try {
    const existing = localStorage.getItem(SESSION_ID_KEY);
    if (existing) return existing;
    const id = crypto.randomUUID();
    localStorage.setItem(SESSION_ID_KEY, id);
    return id;
  } catch {
    // SSR / incognito fallback
    return crypto.randomUUID();
  }
}
function loadConversations(): Conversation[] {
  if (typeof window === 'undefined') return [];
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]'); } catch { return []; }
}
function saveConversations(convs: Conversation[]) {
  if (typeof window === 'undefined') return;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(convs.slice(0, 50)));
}

// ─── Markdown renderer ────────────────────────────────────────────────────────
function renderMd(text: string): string {
  return text
    .replace(/^### (.+)$/gm, '<h3 class="md-h3">$1</h3>')
    .replace(/^## (.+)$/gm,  '<h2 class="md-h2">$1</h2>')
    .replace(/^# (.+)$/gm,   '<h1 class="md-h1">$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code class="md-code">$1</code>')
    .replace(/^\|(.+)\|$/gm, (row) => {
      if (/^[\s|:-]+$/.test(row)) return '<!--sep-->';
      const cells = row.split('|').filter(Boolean).map(c => `<td class="md-td">${c.trim()}</td>`).join('');
      return `<tr>${cells}</tr>`;
    })
    .replace(
      /((?:<tr>.*?<\/tr>\n?))(<!--sep-->\n)?((?:<tr>.*?<\/tr>\n?)*)/gs,
      (_: string, firstRow: string, sep: string, restRows: string) => {
        if (!firstRow.trim()) return _;
        if (sep) {
          const head = firstRow.replace(/<td class="md-td">/g, '<th class="md-th">').replace(/<\/td>/g, '<\/th>');
          return `<table class="md-table"><thead>${head}<\/thead><tbody>${restRows}<\/tbody><\/table>`;
        }
        return `<table class="md-table"><tbody>${firstRow}${restRows}<\/tbody><\/table>`;
      }
    )
    .replace(/^\s*[-*+]\s+(.+)$/gm, '<li class="md-li">$1</li>')
    .replace(/(<li[\s\S]*?<\/li>\n?)+/g, m => `<ul class="md-ul">${m}</ul>`)
    .replace(/\[(\d+)\]/g, '<sup class="md-ref">[$1]</sup>')
    .replace(/\n\n/g, '</p><p class="md-p">')
    .replace(/\n/g, '<br/>')
    .replace(/^(?!<)/, '<p class="md-p">')
    .replace(/(?<!>)$/, '</p>');
}

// ─── Chart validation ────────────────────────────────────────────────────────
function isValidChart(spec: ChartSpec): boolean {
  const allPts = spec.series.flatMap(s => s.data ?? []);
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
  const data = spec.series[0]?.data ?? [];
  if (data.length < 2) return null;
  const absVals = data.map(d => Math.abs(d.value));
  const max = Math.max(...absVals, 1);
  const W = 320, H = 150, pad = 32;
  const barW = Math.max(8, Math.floor((W - pad * 2) / data.length - 6));
  const COLORS = ['#1a1f4e','#3b82f6','#22c55e','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#ec4899'];
  // Y-axis grid lines
  const gridVals = [0.25, 0.5, 0.75, 1.0];
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
              <text x={x+barW/2} y={H-6} textAnchor="middle" fontSize="8" fill="#8b93b5" fontFamily="DM Sans,sans-serif">
                {d.label.length > 9 ? d.label.slice(0,9)+ '…' : d.label}
              </text>
              <text x={x+barW/2} y={isNeg ? H-22+bH+10 : H-22-bH-4} textAnchor="middle" fontSize="8" fill={color} fontFamily="DM Mono,monospace" fontWeight="700">
                {spec.unit==='%' ? `${d.value>0?'+':''}${d.value.toFixed(2)}%` : d.value.toLocaleString('en-IN')}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function LineChart({ spec }: { spec: ChartSpec }) {
  const W = 320, H = 130, pad = 32;
  const allV = spec.series.flatMap(s => s.data.map(d => d.value));
  // Need at least 2 distinct time points to draw a meaningful line
  const allLabels = spec.series.flatMap(s => s.data.map(d => d.label));
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
  const labels = spec.series[0]?.data ?? [];
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
        {spec.series.map((s,si) => (
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
      {spec.series.length > 1 && (
        <div className="chart-legend">
          {spec.series.map((s,i) => (
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
  const data = spec.series[0]?.data ?? [];
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

function ChartBlock({ spec }: { spec: ChartSpec }) {
  // Hard gate: never render a chart that has bad/useless data
  if (!isValidChart(spec)) return null;
  if (spec.type === 'pie') return <PieChart spec={spec}/>;
  if (spec.type === 'line') return <LineChart spec={spec}/>;
  return <BarChart spec={spec}/>;
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
function ReportPanel({ msg, question }: { msg: Message; question: string }) {
  const [open, setOpen]               = useState(false);
  const [pdfLoading, setPdfLoading]   = useState(false);
  const [emailOpen, setEmailOpen]     = useState(false);
  const [emailSending, setEmailSending] = useState(false);
  const [emailResult, setEmailResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const rd = msg.reportData;
  const loading = msg.reportLoading ?? false;
  const done = !!rd;

  if (!loading && !done) return null;

  const downloadPdf = async () => {
    if (!rd || pdfLoading) return;
    setPdfLoading(true);
    try {
      const res = await fetch('/api/chat/report/pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ report: rd.report, charts: rd.charts, question, keyStats: rd.keyStats, summary: rd.summary, fileImages: rd.fileImages ?? [] }),
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

  const sendEmail = async (subject: string, recipients: string, file: File | null) => {
    if (!rd) return;
    setEmailSending(true);
    setEmailResult(null);
    try {
      const fd = new FormData();
      fd.append('subject',    subject || 'Growth Gradual Research Report');
      fd.append('recipients', recipients);
      fd.append('report',     rd.report   ?? '');
      fd.append('title',      question.slice(0, 120));
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

  const defaultSubject = `Growth Gradual Research Report — ${question.slice(0, 60)}${question.length > 60 ? '…' : ''}`;

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
              {rd.charts.length > 0 && (
                <div className="charts-grid">
                  {rd.charts.map((c,i) => <ChartBlock key={i} spec={c}/>)}
                </div>
              )}
              <div dangerouslySetInnerHTML={{ __html: renderMd(rd.report) }}/>
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
  // Conversations (sidebar history)
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId]           = useState<string | null>(null);
  const [messages, setMessages]           = useState<Message[]>([]);
  const [sidebarOpen, setSidebarOpen]     = useState(true);

  // Chat state
  const [input, setInput]       = useState('');
  const [streaming, setStreaming] = useState(false);
  const [searching, setSearching] = useState(false);
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

    // Process each file and update its chip as it completes
    await Promise.all(toProcess.map(async (file, idx) => {
      const placeholder = placeholders[idx];
      try {
        const result = await processFile(file);
        setAttachedFiles(prev => prev.map(f =>
          f.id === placeholder.id
            ? (result ? { ...result, id: placeholder.id } : { ...placeholder, status: 'failed' as const, error: 'Unsupported file type' })
            : f
        ));
      } catch (e) {
        setAttachedFiles(prev => prev.map(f =>
          f.id === placeholder.id
            ? { ...placeholder, status: 'failed' as const, error: (e as Error).message }
            : f
        ));
      }
    }));

    setAttachLoading(false);

    // ── Upload files through Paperly pipeline (file-service → extraction-service → rag-service) ──
    // This gives us proper OCR via Gemini Vision, Supabase storage, and RAG indexing all in one step.
    const sessionId = getOrCreateSessionId();
    setRagIndexing(true);
    try {
      const fd = new FormData();
      fd.append('sessionId', sessionId);
      for (const file of toProcess) {
        fd.append('files', file, file.name);
      }
      const res = await fetch('/api/upload', {
        method: 'POST',
        body: fd,
        signal: AbortSignal.timeout(300_000), // 5 min — OCR can be slow
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        setRagIndexed(true);
        console.log('[Upload] Paperly pipeline succeeded:', data);
      } else {
        console.warn('[Upload] Paperly pipeline error:', res.status, data);
        // Fall back to local RAG index if extraction-service is unavailable
        const processed = await Promise.all(
          placeholders.map(ph => new Promise<AttachedFile | null>(resolve => {
            setAttachedFiles(prev => {
              const f = prev.find(x => x.id === ph.id);
              resolve(f && f.status === 'attached' ? f : null);
              return prev;
            });
          }))
        );
        const toIndex = processed.filter((f): f is AttachedFile => f !== null && !!f.extractedText);
        if (toIndex.length > 0) {
          const docs = toIndex.map(f => ({
            id: f.id, name: f.name,
            text: f.extractedText!,
            source_type: 'file',
            file_type: f.type,
          }));
          const r2 = await fetch('/api/rag/index', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, documents: docs }),
          });
          const d2 = await r2.json();
          if (d2.chunks_added > 0 || d2.total_chunks > 0) {
            setRagIndexed(true);
            console.log('[RAG] fallback indexed', d2.chunks_added, 'chunks');
          }
        }
      }
    } catch (e) {
      console.warn('[Upload] pipeline failed (non-critical):', e);
    } finally {
      setRagIndexing(false);
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
    setTimeout(() => inputRef.current?.focus(), 100);
  }, []);

  // Load a conversation
  const loadConversation = useCallback((conv: Conversation) => {
    abortRef.current?.abort();
    setMessages(conv.messages);
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

    setMessages(newMessages);
    setStreaming(true);
    setSearching(true);
    historyRef.current = [...historyRef.current, { role:'user', content:q }];

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let metaDone = false;
    let finalText = '';
    const fileCtx = buildAttachmentContext(attachedFiles, pastedTexts);

    try {
      let acc = '';
      for await (const chunk of streamReply(historyRef.current, ctrl.signal, (meta) => {
        metaDone = true;
        setSearching(false);
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
      }, fileCtx, getOrCreateSessionId(), ragIndexed)) {
        if (!metaDone) { setSearching(false); metaDone = true; }
        acc += chunk;
        finalText = acc;
        setMessages(prev => prev.map(m => m.id === botMsg.id ? { ...m, text:acc } : m));
      }
      historyRef.current = [...historyRef.current, { role:'assistant', content:finalText }];

      // Auto-generate report in background after stream completes
      const botMsgId = botMsg.id;
      const currentFiles = attachedFiles;
      setMessages(prev => prev.map(m => m.id === botMsgId ? { ...m, reportLoading: true } : m));

      // Build file images for report (render PDF pages / pass images as base64)
      const buildFilePayload = async () => {
        const fileImages: { name: string; mimeType: string; data: string }[] = [];
        const fileTextParts: string[] = [];

        for (const f of currentFiles) {
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

      buildFilePayload().then(({ fileImages, fileTextContext }) => {
        return fetch('/api/chat/report', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            question:    q,
            sources:     [],
            fileContext: fileTextContext,
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
              charts:     data.charts     ?? [],
              keyStats:   data.keyStats   ?? [],
              summary:    data.summary    ?? '',
              fileImages: data.fileImages ?? [],
            },
          } : m));
        })
        .catch(() => {
          setMessages(prev => prev.map(m => m.id === botMsgId ? { ...m, reportLoading: false } : m));
        });

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
    }
  }, [streaming, messages, activeId, attachedFiles, pastedTexts]);

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
        /* ── Layout ─────────────────────────────────────────────────────────── */
        .chat-shell {
          display: flex;
          height: calc(100vh - 112px);
          min-height: 520px;
          background: #ffffff;
          border-radius: 16px;
          border: 1px solid #e2e6f0;
          box-shadow: 0 4px 24px rgba(26,31,78,.07);
          overflow: hidden;
          font-family: 'DM Sans', sans-serif;
          position: relative;
        }

        /* ── Sidebar ────────────────────────────────────────────────────────── */
        .sidebar {
          width: 260px;
          flex-shrink: 0;
          background: #f0f2f7;
          border-right: 1px solid #e2e6f0;
          display: flex;
          flex-direction: column;
          transition: width .25s cubic-bezier(.4,0,.2,1);
          overflow: hidden;
        }
        .sidebar--closed { width: 0; }

        .sidebar-hdr {
          padding: 16px 14px 12px;
          display: flex;
          align-items: center;
          gap: 10px;
          border-bottom: 1px solid #e2e6f0;
          flex-shrink: 0;
        }
        .sidebar-logo {
          width: 34px; height: 34px; border-radius: 9px;
          background: #ffffff;
          display: flex; align-items: center; justify-content: center;
          flex-shrink: 0; overflow: hidden;
          border: 1px solid #e2e6f0;
          box-shadow: 0 1px 4px rgba(26,31,78,.08);
        }
        .sidebar-brand { color: #1a1f4e; font-size: 13px; font-weight: 700; font-family: 'Playfair Display',serif; line-height:1.2; white-space:nowrap; }
        .sidebar-brand span { display:block; font-size:9px; font-weight:400; color:#8b93b5; font-family:'DM Sans',sans-serif; letter-spacing:.06em; text-transform:uppercase; }

        .new-chat-btn {
          margin: 10px 12px;
          display: flex; align-items: center; gap: 8px;
          padding: 9px 13px;
          border-radius: 9px;
          border: 1px solid #e2e6f0;
          background: #ffffff;
          color: #1a1f4e;
          font-size: 12px; font-family: 'DM Sans',sans-serif;
          cursor: pointer;
          transition: background .15s, box-shadow .15s;
          flex-shrink: 0;
          white-space: nowrap;
          box-shadow: 0 1px 3px rgba(26,31,78,.07);
        }
        .new-chat-btn:hover { background: #e8eaf2; box-shadow: 0 2px 6px rgba(26,31,78,.1); }

        .conv-list {
          flex: 1; overflow-y: auto; padding: 4px 0 12px;
          scrollbar-width: thin; scrollbar-color: #d0d5e8 transparent;
        }
        .conv-list::-webkit-scrollbar { width: 3px; }
        .conv-list::-webkit-scrollbar-thumb { background: #d0d5e8; border-radius: 4px; }

        .conv-group-label {
          padding: 10px 14px 4px;
          font-size: 9px; color: #b0b8d4;
          text-transform: uppercase; letter-spacing: .1em;
          font-family: 'DM Sans',sans-serif;
          white-space: nowrap;
        }
        .conv-item {
          display: flex; align-items: center; gap: 8px;
          padding: 8px 12px;
          cursor: pointer;
          border-radius: 8px;
          margin: 1px 6px;
          transition: background .13s;
          position: relative;
        }
        .conv-item:hover { background: #e2e6f0; }
        .conv-item--active { background: #e2e6f0; }
        .conv-item__icon { flex-shrink:0; opacity:.45; }
        .conv-item__title {
          flex: 1; font-size: 12px; color: #4b5680;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
          font-family: 'DM Sans',sans-serif;
        }
        .conv-item--active .conv-item__title { color: #1a1f4e; font-weight: 600; }
        .conv-del {
          opacity: 0; width:22px; height:22px; border-radius:6px;
          border: none; background: rgba(26,31,78,.07); color:#8b93b5;
          cursor:pointer; display:flex; align-items:center; justify-content:center;
          flex-shrink:0; transition: opacity .15s, background .15s;
        }
        .conv-item:hover .conv-del { opacity:1; }
        .conv-del:hover { background:rgba(239,68,68,.12); color:#ef4444; }

        .sidebar-footer {
          padding: 10px 14px 14px;
          border-top: 1px solid #e2e6f0;
          font-size: 10px; color: #b0b8d4;
          font-family: 'DM Sans',sans-serif;
          flex-shrink: 0;
          white-space: nowrap;
        }

        /* ── Main chat area ─────────────────────────────────────────────────── */
        .chat-main {
          flex: 1; display: flex; flex-direction: column; min-width: 0;
          background: #f8f9fc;
        }

        /* ── Top bar ────────────────────────────────────────────────────────── */
        .chat-topbar {
          display: flex; align-items: center; gap: 10px;
          padding: 11px 18px 10px;
          background: #ffffff;
          border-bottom: 1px solid #e2e6f0;
          flex-shrink: 0;
        }
        .topbar-toggle {
          width: 30px; height: 30px; border-radius: 8px;
          border: 1px solid #e2e6f0; background: #f8f9fc;
          color: #8b93b5; cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          transition: background .15s, color .15s; flex-shrink:0;
        }
        .topbar-toggle:hover { background: #f0f2f7; color: #1a1f4e; }
        .topbar-logo {
          width: 32px; height: 32px; border-radius: 9px;
          background: #1a1f4e; overflow:hidden;
          display:flex; align-items:center; justify-content:center;
          flex-shrink:0;
          border: 1px solid rgba(26,31,78,.15);
        }
        .topbar-info { flex:1; min-width:0; }
        .topbar-name { font-size:14px; font-weight:700; color:#1a1f4e; font-family:'Playfair Display',serif; }
        .topbar-sub { font-size:10px; color:#8b93b5; display:flex; align-items:center; gap:5px; }
        .topbar-dot { width:6px;height:6px;border-radius:50%;background:#22c55e;box-shadow:0 0 5px #22c55e;animation:blink 2s ease-in-out infinite; }
        @keyframes blink { 0%,100%{opacity:1;}50%{opacity:.3;} }
        .topbar-badge { font-size:9px; background:#eff6ff; border:1px solid #bfdbfe; color:#1d4ed8; padding:3px 9px; border-radius:20px; display:flex; align-items:center; gap:4px; white-space:nowrap; }

        /* ── Messages ───────────────────────────────────────────────────────── */
        .chat-msgs {
          flex:1; overflow-y:auto; padding:28px 0 16px;
          display:flex; flex-direction:column; gap:0;
          scrollbar-width:thin; scrollbar-color:#e2e6f0 transparent;
        }
        .chat-msgs::-webkit-scrollbar { width:4px; }
        .chat-msgs::-webkit-scrollbar-thumb { background:#e2e6f0; border-radius:4px; }

        /* Message rows */
        .msg-row { padding: 6px 0; display:flex; }
        .msg-row--user { justify-content: flex-end; }
        .msg-row--bot  { justify-content: flex-start; }

        /* Inner container to constrain width */
        .msg-inner {
          display:flex; gap:12px; align-items:flex-start;
          max-width: min(780px, 88%);
          padding: 0 24px;
          width:100%;
        }
        .msg-row--user .msg-inner { flex-direction:row-reverse; margin-left:auto; }
        .msg-row--bot  .msg-inner { margin-right:auto; }

        /* Avatar */
        .msg-avatar {
          width:32px; height:32px; border-radius:10px; flex-shrink:0;
          display:flex; align-items:center; justify-content:center;
          margin-top:2px; overflow:hidden;
        }
        .msg-avatar--bot { background:#1a1f4e; border:1px solid rgba(26,31,78,.15); }
        .msg-avatar--user { background:#e2e6f0; border:1px solid #d0d5e8; }

        /* Bubble */
        .msg-bubble { display:flex; flex-direction:column; gap:4px; min-width:0; flex:1; }
        .msg-text {
          padding:12px 16px; border-radius:18px;
          font-size:13.5px; line-height:1.68; word-break:break-word;
        }
        .msg-row--user .msg-text {
          background:#1a1f4e; color:#fff;
          border-bottom-right-radius:5px;
        }
        .msg-row--bot .msg-text {
          background:#ffffff; color:#1a1f4e;
          border:1px solid #e2e6f0;
          border-bottom-left-radius:5px;
          box-shadow:0 1px 4px rgba(26,31,78,.05);
        }
        .msg-ts { font-size:10px; color:#b0b8d4; padding:0 4px; }
        .msg-row--user .msg-ts { text-align:right; }


        /* Markdown inside bubbles */
        .msg-text .md-p { margin:0 0 8px; }
        .msg-text .md-p:last-child { margin-bottom:0; }
        .msg-text .md-h1,.msg-text .md-h2,.msg-text .md-h3 { font-family:'Playfair Display',serif; color:#1a1f4e; margin:11px 0 5px; }
        .msg-text .md-h1{font-size:17px;} .msg-text .md-h2{font-size:15px;} .msg-text .md-h3{font-size:13.5px;}
        .msg-text .md-ul { margin:4px 0 8px 16px; padding:0; list-style:disc; }
        .msg-text .md-li { margin:3px 0; }
        .msg-text .md-code { background:rgba(26,31,78,.06); padding:1px 6px; border-radius:4px; font-family:'DM Mono',monospace; font-size:12px; }
        .msg-text .md-ref { color:#3b82f6; font-size:10px; }
        .msg-text .md-table { border-collapse:collapse; margin:8px 0; width:100%; font-size:12px; }
        .msg-text .md-table thead tr { background:#1a1f4e; }
        .msg-text .md-table th { background:#1a1f4e; color:#fff; font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; border:1px solid #2d3a6e; padding:6px 9px; text-align:left; }
        .msg-text .md-th { background:#1a1f4e; color:#fff; font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; border:1px solid #2d3a6e; padding:6px 9px; text-align:left; }
        .msg-text .md-td { border:1px solid #e2e6f0; padding:5px 9px; }
        .msg-text .md-table tbody tr:nth-child(even) td { background:#f8f9fc; }

        /* Searching indicator */
        .searching {
          display:flex; align-items:center; gap:7px;
          padding:10px 14px; background:#eff6ff; border:1px solid #bfdbfe;
          border-radius:12px; font-size:12px; color:#1d4ed8; border-bottom-left-radius:4px;
        }
        .searching svg { animation:spin 1.2s linear infinite; }
        @keyframes spin { to{transform:rotate(360deg);} }
        .search-dots { display:flex; gap:3px; }
        .search-dots span { width:4px;height:4px;border-radius:50%;background:#3b82f6;animation:bd .9s ease-in-out infinite; }
        .search-dots span:nth-child(2){animation-delay:.15s;}
        .search-dots span:nth-child(3){animation-delay:.3s;}
        @keyframes bd{0%,60%,100%{transform:translateY(0);}30%{transform:translateY(-5px);}}

        /* Typing dots */
        .typing-wrap { display:flex; align-items:center; gap:12px; padding:6px 0; }
        .typing-dots { display:flex; gap:4px; padding:12px 16px; background:#fff; border:1px solid #e2e6f0; border-radius:18px; border-bottom-left-radius:5px; box-shadow:0 1px 4px rgba(26,31,78,.05); }
        .typing-dot { width:6px;height:6px;border-radius:50%;background:#1a1f4e;animation:bd .9s ease-in-out infinite; }
        .typing-dot:nth-child(2){animation-delay:.15s;} .typing-dot:nth-child(3){animation-delay:.3s;}

        /* Sources */


        /* Report */
        .report-wrap { margin-top:7px; }
        .report-btn { display:flex;align-items:center;gap:5px;background:#1a1f4e;border:none;border-radius:7px;padding:6px 12px;font-size:11px;color:#fff;cursor:pointer;font-family:'DM Sans',sans-serif;transition:opacity .15s; }
        .report-btn:hover { opacity:.85; }
        .report-body { margin-top:8px;background:#f8f9fc;border:1px solid #e2e6f0;border-radius:12px;padding:16px;max-height:560px;overflow-y:auto; }
        .report-loading { display:flex;align-items:center;gap:8px;font-size:12px;color:#8b93b5;font-family:'DM Sans',sans-serif; }
        .dots { display:inline-flex;gap:4px; }
        .dots i { width:6px;height:6px;border-radius:50%;background:#8b93b5;animation:bd .9s ease-in-out infinite;display:block; }
        .dots i:nth-child(2){animation-delay:.15s;} .dots i:nth-child(3){animation-delay:.3s;}
        .report-content { font-size:13px;line-height:1.65;color:#1a1f4e;font-family:'DM Sans',sans-serif; }
        .report-content .md-p{margin:0 0 10px;}
        .report-content .md-h1,.report-content .md-h2,.report-content .md-h3{font-family:'Playfair Display',serif;color:#1a1f4e;margin:14px 0 6px;}
        .report-content .md-h1{font-size:18px;} .report-content .md-h2{font-size:15px;} .report-content .md-h3{font-size:13px;}
        .report-content .md-ul{margin:4px 0 10px 16px;list-style:disc;}
        .report-content .md-li{margin:3px 0;}
        .report-content .md-table{border-collapse:collapse;margin:10px 0;width:100%;font-size:12px;}
        .report-content .md-table th{background:#1a1f4e;color:#fff;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;border:1px solid #2d3a6e;padding:7px 10px;text-align:left;}
        .report-content .md-th{background:#1a1f4e;color:#fff;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;border:1px solid #2d3a6e;padding:7px 10px;text-align:left;}
        .report-content .md-td{border:1px solid #e2e6f0;padding:6px 10px;}
        .report-content .md-table tbody tr:nth-child(even) td{background:#f8f9fc;}

        /* Charts */
        .charts-grid { display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-bottom:18px; }
        .key-stats-row { display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px; }
        .key-stat-card { background:#fff;border:1px solid #e2e6f0;border-radius:10px;padding:12px 16px;min-width:100px;flex:1; }
        .key-stat-label { font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#8b93b5;margin-bottom:4px; }
        .key-stat-value { font-size:17px;font-weight:700;color:#1a1f4e;line-height:1; }
        .key-stat-change { font-size:11px;margin-top:4px;font-weight:600; }
        .key-stat-change.pos { color:#16a34a; } .key-stat-change.neg { color:#dc2626; }
        .chart-wrap { background:#fff;border:1px solid #e2e6f0;border-radius:10px;padding:14px 14px 8px; }
        .chart-title { font-size:11px;font-weight:600;color:#1a1f4e;font-family:'DM Sans',sans-serif;margin-bottom:8px; }
        .chart-legend { display:flex;flex-wrap:wrap;gap:8px;margin-top:6px; }
        .chart-leg { display:flex;align-items:center;gap:4px;font-size:10px;color:#4b5680;font-family:'DM Sans',sans-serif; }
        .chart-leg i { width:8px;height:8px;border-radius:2px;display:block; }

        /* Empty / welcome */
        .chat-welcome {
          flex:1; display:flex; flex-direction:column;
          align-items:center; justify-content:center;
          gap:24px; padding:32px 24px; text-align:center;
        }
        .welcome-logo {
          width:80px;height:80px;border-radius:22px;background:#1a1f4e;
          display:flex;align-items:center;justify-content:center;
          box-shadow:0 8px 28px rgba(26,31,78,.22);overflow:hidden;
        }
        .welcome-title { font-size:22px;font-weight:700;color:#1a1f4e;font-family:'Playfair Display',serif;margin:0; }
        .welcome-sub { font-size:13px;color:#4b5680;line-height:1.6;margin:6px 0 0;max-width:400px; }
        .sugs { display:grid;grid-template-columns:repeat(4,1fr);gap:8px;width:100%;max-width:700px; }
        @media(max-width:640px){.sugs{grid-template-columns:repeat(2,1fr);}}
        .sug {
          display:flex;flex-direction:column;align-items:flex-start;gap:5px;
          padding:12px 13px;border-radius:12px;border:1px solid #e2e6f0;
          background:#fff;cursor:pointer;font-size:12px;color:#1a1f4e;
          text-align:left;transition:all .15s;font-family:'DM Sans',sans-serif;line-height:1.4;
        }
        .sug:hover { border-color:rgba(26,31,78,.3);background:#f0f2f7;transform:translateY(-2px);box-shadow:0 4px 12px rgba(26,31,78,.08); }
        .sug-icon { font-size:18px; }

        /* Input area */
        .chat-input-area {
          padding: 12px 24px 18px;
          background: #ffffff;
          border-top: 1px solid #e2e6f0;
          flex-shrink: 0;
          display:flex;flex-direction:column;gap:8px;
        }
        .input-row {
          display:flex;align-items:flex-end;gap:10px;
          background:#f8f9fc;border:1.5px solid #e2e6f0;border-radius:16px;
          padding:10px 12px 10px 18px;
          transition:border-color .15s,box-shadow .15s;
          max-width:780px;margin:0 auto;width:100%;
        }
        .input-row:focus-within { border-color:rgba(26,31,78,.4);box-shadow:0 0 0 3px rgba(26,31,78,.06); }
        .chat-textarea {
          flex:1;background:none;border:none;outline:none;
          color:#1a1f4e;font-size:14px;font-family:'DM Sans',sans-serif;
          line-height:1.5;resize:none;max-height:130px;min-height:22px;
          scrollbar-width:none;
        }
        .chat-textarea::placeholder { color:#b0b8d4; }
        .chat-textarea::-webkit-scrollbar{display:none;}
        .send-btn {
          width:38px;height:38px;border-radius:11px;border:none;
          background:#1a1f4e;color:#fff;cursor:pointer;
          display:flex;align-items:center;justify-content:center;
          flex-shrink:0;transition:opacity .15s,transform .15s;
          box-shadow:0 2px 8px rgba(26,31,78,.3);
        }
        .send-btn:disabled{opacity:.22;cursor:not-allowed;}
        .send-btn:not(:disabled):hover{transform:scale(1.08);opacity:.9;}
        .send-btn:not(:disabled):active{transform:scale(.93);}
        .input-hint { font-size:10px;color:#b0b8d4;text-align:center;font-family:'DM Sans',sans-serif; }

        /* ── File chips ──────────────────────────────────────────────────── */
        .chips-row {
          display:flex;flex-wrap:wrap;gap:6px;
          padding:10px 18px 4px;
          max-height:90px;overflow-y:auto;
        }
        .file-chip {
          display:flex;align-items:center;gap:5px;
          padding:4px 6px 4px 9px;border-radius:20px;
          background:#f0f3ff;border:1px solid #c8d0e8;
          font-family:'DM Sans',sans-serif;flex-shrink:0;max-width:220px;
        }
        .file-chip--text {
          background:#f0fdf4;border-color:rgba(34,197,94,0.35);
        }
        .chip-dot { width:7px;height:7px;border-radius:50%;flex-shrink:0; }
        .chip-name { font-size:11.5px;color:#1a1f4e;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:130px; }
        .chip-size { font-size:10px;color:#8b93b5;font-family:'DM Mono',monospace;flex-shrink:0; }
        .chip-x {
          width:16px;height:16px;border-radius:50%;border:none;
          background:transparent;color:#ef4444;cursor:pointer;
          display:flex;align-items:center;justify-content:center;
          font-size:14px;line-height:1;font-weight:700;flex-shrink:0;
          opacity:0.55;transition:opacity .15s,background .15s;padding:0;
        }
        .chip-x:hover { opacity:1;background:rgba(239,68,68,0.12); }
        @keyframes spin { to { transform:rotate(360deg); } }
        .chip-spinner {
          width:12px;height:12px;border-radius:50%;flex-shrink:0;
          border:2px solid #c8d0e8;border-top-color:#1a1f4e;
          animation:spin .7s linear infinite;
        }

        /* ── Attach button ───────────────────────────────────────────────── */
        .attach-btn {
          width:32px;height:32px;border-radius:9px;border:1px solid #e2e6f0;
          background:#f8f9fc;color:#8b93b5;cursor:pointer;flex-shrink:0;
          display:flex;align-items:center;justify-content:center;
          transition:background .15s,color .15s,border-color .15s;
        }
        .attach-btn:hover:not(:disabled) { background:#1a1f4e;color:#fff;border-color:#1a1f4e; }
        .attach-btn:disabled { opacity:0.35;cursor:not-allowed; }

        /* ── Drag highlight ──────────────────────────────────────────────── */
        .chat-shell--drag { outline:2.5px dashed #1a1f4e;outline-offset:-2px; }
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
        <aside className={`sidebar${sidebarOpen ? '' : ' sidebar--closed'}`}>
          {/* Sidebar header */}
          <div className="sidebar-hdr">
            <div className="sidebar-logo">
              <Image src="/growth-gradual-icon-transparent.jpeg" alt="Growth Gradual" width={32} height={32} style={{ objectFit:'contain' }}/>
            </div>
            <div className="sidebar-brand">
              Growth Gradual
              <span>In The Money</span>
            </div>
          </div>

          {/* New chat button */}
          <button className="new-chat-btn" onClick={startNewChat}>
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
                      onClick={() => loadConversation(conv)}
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
              <Image src="/growth-gradual-icon-transparent.jpeg" alt="Growth Gradual" width={30} height={30} style={{ objectFit:'contain' }}/>
            </div>
            <div className="topbar-info">
              <div className="topbar-name">Growth Gradual</div>
              <div className="topbar-sub">
                <span className="topbar-dot"/>
                In The Money · Indian Markets AI
              </div>
            </div>
            <div className="topbar-badge">
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
              Web search enabled
            </div>
          </div>

          {/* Messages */}
          <div className="chat-msgs">
            {isEmpty ? (
              <div className="chat-welcome">
                <div className="welcome-logo">
                  <Image src="/growth-gradual-icon-transparent.jpeg" alt="Growth Gradual" width={68} height={68} style={{ objectFit:'contain' }}/>
                </div>
                <div>
                  <p className="welcome-title">Growth Gradual</p>
                  <p className="welcome-sub">Your AI analyst for Indian markets — powered by live web search. Ask anything about stocks, MFs, macro, RBI, IPOs and more.</p>
                </div>
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

                // Searching state
                if (isLast && streaming && !isUser && searching) return (
                  <div key={msg.id} className="msg-row msg-row--bot">
                    <div className="msg-inner">
                      <div className="msg-avatar msg-avatar--bot">
                        <Image src="/growth-gradual-icon-transparent.png" alt="" width={28} height={28} style={{ objectFit:'contain' }}/>
                      </div>
                      <div className="msg-bubble">
                        <div className="searching">
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                          Searching the web…
                          <div className="search-dots"><span/><span/><span/></div>
                        </div>
                      </div>
                    </div>
                  </div>
                );

                // Typing dots
                if (isLast && streaming && !isUser && !msg.text) return (
                  <div key={msg.id} className="msg-row msg-row--bot">
                    <div className="msg-inner">
                      <div className="msg-avatar msg-avatar--bot">
                        <Image src="/growth-gradual-icon-transparent.png" alt="" width={28} height={28} style={{ objectFit:'contain' }}/>
                      </div>
                      <div className="msg-bubble">
                        <div className="typing-dots">
                          <div className="typing-dot"/><div className="typing-dot"/><div className="typing-dot"/>
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
                          <Image src="/growth-gradual-icon-transparent.png" alt="" width={28} height={28} style={{ objectFit:'contain' }}/>
                        )}
                      </div>
                      {/* Bubble */}
                      <div className="msg-bubble">

                        <div
                          className="msg-text"
                          dangerouslySetInnerHTML={{ __html: isUser ? msg.text.replace(/\n/g,'<br/>') : renderMd(msg.text) }}
                        />

                        {!isUser && (msg.reportLoading || msg.reportData) && (
                          <ReportPanel msg={msg} question={msg.text.slice(0,300)}/>
                        )}
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
              <div className="chips-row">
                {attachedFiles.map(f => (
                  <div key={f.id} className="file-chip" title={f.status === 'failed' ? (f.error || 'Failed to read file') : f.name}
                    style={{
                      borderColor: f.status === 'failed' ? '#fca5a5' : f.status === 'attaching' ? '#c4b5fd' : undefined,
                      background:  f.status === 'failed' ? '#fef2f2' : f.status === 'attaching' ? '#f5f3ff' : undefined,
                    }}>
                    {f.status === 'attaching'
                      ? <div className="chip-spinner"/>
                      : <span className="chip-dot" style={{ background: f.status === 'failed' ? '#ef4444' : f.type.startsWith('image/') ? '#22c55e' : f.type === 'application/pdf' ? '#ef4444' : '#3b82f6' }} />
                    }
                    <span className="chip-name">{f.name.length > 18 ? f.name.slice(0,15)+'…' : f.name}</span>
                    <span className="chip-size" style={{
                      color: f.status === 'failed' ? '#dc2626' : f.status === 'attaching' ? '#7c3aed' : undefined,
                      fontWeight: f.status !== 'attached' ? 600 : undefined,
                    }}>
                      {f.status === 'attaching' ? 'reading…'
                        : f.status === 'failed' ? 'failed'
                        : f.extractedText ? `${wordCount(f.extractedText).toLocaleString()}w`
                        : fmtSize(f.size)}
                    </span>
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
                {ragIndexing && (
                  <div className="file-chip" style={{ borderColor:'#c4b5fd', background:'#f5f3ff' }}>
                    <div className="chip-spinner"/>
                    <span className="chip-name">Indexing…</span>
                    <span className="chip-size" style={{ color:'#7c3aed', fontWeight:600 }}>RAG</span>
                  </div>
                )}
                {ragIndexed && !ragIndexing && (
                  <div className="file-chip" style={{ borderColor:'#86efac', background:'#f0fdf4' }}>
                    <span style={{ fontSize:11 }}>🧠</span>
                    <span className="chip-name">RAG ready</span>
                    <span className="chip-size" style={{ color:'#15803d', fontWeight:600 }}>indexed</span>
                  </div>
                )}
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
                  : 'Ask about Nifty, stocks, MFs, RBI, IPOs… or attach files 📎'}
                value={input}
                rows={1}
                onChange={e => {
                  setInput(e.target.value);
                  e.target.style.height = 'auto';
                  e.target.style.height = Math.min(e.target.scrollHeight, 130) + 'px';
                }}
                onKeyDown={onKey}
                onPaste={handlePaste}
                disabled={streaming || attachedFiles.some(f => f.status === 'attaching')}
              />
              <button className="send-btn" onClick={() => send(input)} disabled={!input.trim() || streaming || attachedFiles.some(f => f.status === 'attaching')} aria-label="Send">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M22 2L11 13M22 2L15 22l-4-9-9-4 20-7z"/>
                </svg>
              </button>
            </div>
            <p className="input-hint">
              Growth Gradual · Enter to send · Shift+Enter for newline
              {attachedFiles.length > 0 || pastedTexts.length > 0
                ? ` · ${attachedFiles.length + pastedTexts.length} item${attachedFiles.length + pastedTexts.length !== 1 ? 's' : ''} attached`
                : ' · 📎 to attach files or paste images/text'}
            </p>
          </div>

        </div>
      </div>
    </>
  );
}
