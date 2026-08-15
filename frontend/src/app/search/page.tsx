'use client';
import { useState } from 'react';
import DataDashboard from '@/components/DataDashboard';
import { DataSearchResult } from '@/types';

const EXAMPLES = [
  'Revenue and profit margins for Reliance, TCS, and Infosys',
  'Funding raised by top Indian fintech startups in 2026',
  'P/E ratios of Nifty Bank constituents',
  'AUM growth of top Indian mutual fund houses',
];

export default function DataSearchPage() {
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<DataSearchResult | null>(null);

  const runSearch = async (q?: string) => {
    const term = (q ?? query).trim();
    if (!term || loading) return;
    setQuery(term);
    setLoading(true);
    setError('');
    setResult(null);
    try {
      const res = await fetch('/api/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: term }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || `Search failed (${res.status})`);
      } else {
        setResult(data as DataSearchResult);
      }
    } catch {
      setError('Unable to reach the data search backend. Please try again.');
    }
    setLoading(false);
  };

  return (
    <div style={{ maxWidth: '980px', margin: '0 auto', padding: '28px 16px 60px' }}>
      <div style={{ marginBottom: '22px' }}>
        <p style={{
          fontSize: '10px', fontWeight: 700, color: 'var(--gold)', textTransform: 'uppercase',
          letterSpacing: '2px', fontFamily: "'DM Sans',sans-serif", marginBottom: '6px',
        }}>Data Search Engine</p>
        <h1 style={{
          fontSize: 'clamp(22px,3vw,30px)', fontFamily: "'Playfair Display',serif",
          fontWeight: 700, color: 'var(--navy2)', marginBottom: '6px',
        }}>Turn any question into a dashboard</h1>
        <p style={{ fontSize: '13px', color: 'var(--text2)', fontFamily: "'DM Sans',sans-serif" }}>
          Ask for the numbers you need — live market data and web sources are pulled together into
          data points you can chart, browse, or export to Excel/CSV.
        </p>
      </div>

      <div style={{ display: 'flex', gap: '8px', marginBottom: '10px' }}>
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && runSearch()}
          placeholder="e.g. Revenue and net profit of HDFC Bank and ICICI Bank for FY25"
          style={{
            flex: 1, padding: '13px 16px', fontSize: '13px', background: '#fff',
            border: '1px solid var(--border)', borderRadius: 'var(--radius-md)',
            color: 'var(--text)', fontFamily: "'DM Sans',sans-serif", outline: 'none',
          }}
        />
        <button
          onClick={() => runSearch()}
          disabled={loading || !query.trim()}
          style={{
            padding: '13px 22px', fontSize: '13px', fontWeight: 600, color: '#fff',
            background: 'var(--teal)', border: 'none', borderRadius: 'var(--radius-md)',
            cursor: loading || !query.trim() ? 'default' : 'pointer',
            opacity: loading || !query.trim() ? 0.6 : 1, fontFamily: "'DM Sans',sans-serif",
            whiteSpace: 'nowrap',
          }}
        >{loading ? 'Searching…' : 'Search'}</button>
      </div>

      {!result && !loading && (
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '24px' }}>
          {EXAMPLES.map(ex => (
            <button
              key={ex}
              onClick={() => runSearch(ex)}
              style={{
                fontSize: '11px', padding: '7px 12px', borderRadius: 'var(--radius-pill)',
                border: '1px solid var(--border)', background: '#fff', color: 'var(--text2)',
                cursor: 'pointer', fontFamily: "'DM Sans',sans-serif",
              }}
            >{ex}</button>
          ))}
        </div>
      )}

      {loading && (
        <div style={{
          padding: '40px', textAlign: 'center', color: 'var(--muted)',
          fontFamily: "'DM Sans',sans-serif", fontSize: '13px',
        }}>
          Scanning up to 100 sources across live market data and web search…
        </div>
      )}

      {error && !loading && (
        <div style={{
          padding: '16px 18px', background: '#fef2f2', border: '1px solid #fecaca',
          borderRadius: 'var(--radius-md)', color: '#991b1b', fontSize: '13px',
          fontFamily: "'DM Sans',sans-serif", marginBottom: '20px',
        }}>{error}</div>
      )}

      {result && !loading && <DataDashboard result={result} />}
    </div>
  );
}
