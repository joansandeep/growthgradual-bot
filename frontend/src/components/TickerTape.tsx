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
        const hasRealData = data.stocks?.some((s: LiveQuote) => s.price && s.price !== '—');
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
      background: '#0f172a',
      borderBottom: '1px solid rgba(200,146,42,0.2)',
      overflow: 'hidden',
      height: '30px',
      display: 'flex',
      alignItems: 'center',
    }}>
      {/* Fades on edges */}
      <div style={{ position:'relative', overflow:'hidden', width:'100%', height:'100%', display:'flex', alignItems:'center' }}>
        <div style={{
          position:'absolute', left:0, top:0, bottom:0, width:'60px', zIndex:2,
          background:'linear-gradient(90deg,#0f172a,transparent)',
          pointerEvents:'none',
        }}/>
        <div style={{
          position:'absolute', right:0, top:0, bottom:0, width:'60px', zIndex:2,
          background:'linear-gradient(270deg,#0f172a,transparent)',
          pointerEvents:'none',
        }}/>
        <div className="ticker-track" style={{ display:'flex', gap:'0', width:'max-content' }}>
          {doubled.map((s, i) => (
            <div key={i} style={{ display:'flex', alignItems:'center', gap:'8px', flexShrink:0, padding:'0 24px 0 0' }}>
              <span style={{
                fontSize:'9.5px', fontFamily:'JetBrains Mono,monospace',
                color:'rgba(255,255,255,0.5)', fontWeight:600, letterSpacing:'0.6px',
              }}>{s.symbol}</span>
              <span style={{
                fontSize:'9.5px', fontFamily:'JetBrains Mono,monospace',
                color:'rgba(255,255,255,0.88)', fontWeight:600,
              }}>{s.price}</span>
              <span style={{
                fontSize:'8.5px', fontFamily:'JetBrains Mono,monospace',
                color: s.change === '—' ? 'rgba(255,255,255,0.2)'
                     : s.up ? '#4ade80' : '#f87171',
                background: s.change === '—' ? 'transparent'
                          : s.up ? 'rgba(74,222,128,0.1)' : 'rgba(248,113,113,0.1)',
                padding:'1px 5px', borderRadius:'2px',
              }}>
                {s.change === '—' ? '—' : `${s.up ? '▲' : '▼'} ${s.change}`}
              </span>
              <span style={{ color:'rgba(255,255,255,0.1)', fontSize:'8px', paddingLeft:'16px' }}>│</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
