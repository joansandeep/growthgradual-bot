'use client';
import { useEffect, useState } from 'react';
import { Stock } from '@/types';

interface LiveQuote {
  symbol: string;
  price: string;
  change: string;
  up: boolean;
}

export default function TickerTape({ stocks: fallback }: { stocks: Stock[] }) {
  const [stocks, setStocks] = useState<LiveQuote[]>(fallback);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch('/api/market', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        // Only use live data if we actually got real prices (not all dashes)
        const hasRealData = data.stocks?.some((s: LiveQuote) => s.price && s.price !== '—');
        // Accept live AND cached/stale data — stale prices are still useful in ticker
        if (hasRealData) setStocks(data.stocks);
      } catch { /* keep fallback */ }
    };
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);

  const doubled = [...stocks, ...stocks];

  return (
    <div style={{
      background: '#f8fafc',
      borderBottom: '1px solid #e2e8f0',
      overflow: 'hidden',
      padding: '7px 0',
    }}>
      <div className="ticker-track" style={{ display: 'flex', gap: '40px', width: 'max-content' }}>
        {doubled.map((s, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
            <span style={{
              fontSize: '10px', fontFamily: 'JetBrains Mono, monospace',
              color: '#64748b', fontWeight: 500, letterSpacing: '0.5px',
            }}>{s.symbol}</span>
            <span style={{
              fontSize: '10px', fontFamily: 'JetBrains Mono, monospace',
              color: '#1e293b', fontWeight: 500,
            }}>{s.price}</span>
            <span style={{
              fontSize: '9px', fontFamily: 'JetBrains Mono, monospace',
              color: s.change === '—' ? '#94a3b8' : (s.up ? '#15803d' : '#b91c1c'),
              background: s.change === '—' ? 'rgba(148,163,184,0.08)' : (s.up ? 'rgba(21,128,61,0.08)' : 'rgba(185,28,28,0.08)'),
              padding: '1px 5px', borderRadius: '2px',
            }}>{s.change === '—' ? '—' : `${s.up ? '▲' : '▼'} ${s.change}`}</span>
            <span style={{ color: '#e2e8f0', fontSize: '8px' }}>│</span>
          </div>
        ))}
      </div>
    </div>
  );
}
