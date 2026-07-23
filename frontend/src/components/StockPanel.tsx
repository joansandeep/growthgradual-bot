'use client';
import { useEffect, useState, useCallback } from 'react';
import { Stock, QuickStat } from '@/types';

// ── Types that mirror /api/market response ─────────────────────────────────
interface LiveQuote {
  symbol: string;
  price: string;
  change: string;
  up: boolean;
  raw: number;
}
interface LiveStat {
  label: string;
  value: string;
  sub: string;
}
interface MarketData {
  stocks: LiveQuote[];
  stats: LiveStat[];
  gainers: LiveQuote[];
  losers: LiveQuote[];
  marketOpen: boolean;
  fetchedAt: string;
  fromCache: boolean;
  stale?: boolean;
  cacheAge?: string;
  error?: string;
}

// ── Static fallback (same as old data/index.ts values) ────────────────────
const FALLBACK: MarketData = {
  stocks: [
    { symbol: 'NIFTY 50',   price: '—', change: '—',     up: true,  raw: 0 },
    { symbol: 'SENSEX',     price: '—', change: '—',     up: true,  raw: 0 },
    { symbol: 'NIFTY BANK', price: '—', change: '—',     up: false, raw: 0 },
    { symbol: 'NIFTY IT',   price: '—', change: '—',     up: true,  raw: 0 },
    { symbol: 'NIFTY MID',  price: '—', change: '—',     up: true,  raw: 0 },
    { symbol: 'USD/INR',    price: '—', change: '—',     up: true,  raw: 0 },
  ],
  stats: [
    { label: 'USD/INR',        value: '—', sub: '—' },
    { label: 'Gold (MCX ~10g)',value: '—', sub: '—' },
    { label: 'Crude Oil',      value: '—', sub: '—' },
    { label: '10Y Yield (US)', value: '—', sub: '—' },
    { label: 'Repo Rate',      value: '—', sub: 'Loading…' },
  ],
  gainers: [],
  losers: [],
  marketOpen: false,
  fetchedAt: '',
  fromCache: false,
};

// ── Sub-components ─────────────────────────────────────────────────────────
const Panel = ({ children, style = {} }: { children: React.ReactNode; style?: React.CSSProperties }) => (
  <div style={{
    background: '#ffffff',
    border: '1px solid #e6e2d8',
    borderRadius: '10px',
    padding: '16px',
    boxShadow: '0 2px 10px rgba(15,23,42,0.06)',
    ...style,
  }}>{children}</div>
);

const SectionLabel = ({ children }: { children: React.ReactNode }) => (
  <div style={{ display:'flex', alignItems:'center', gap:'6px', marginBottom:'12px', paddingBottom:'8px', borderBottom:'1px solid #f0f2f7' }}>
    <div style={{ width:'2px', height:'12px', background:'linear-gradient(180deg,#0d5c45,#c8922a)', borderRadius:'2px' }}/>
    <p style={{
      fontSize:'9px', fontWeight:700, color:'#8892a4',
      textTransform:'uppercase', letterSpacing:'2px',
      fontFamily:"'DM Sans',sans-serif", margin:0,
    }}>{children}</p>
  </div>
);

function Pulse({ color = '#22c55e' }: { color?: string }) {
  return (
    <span style={{
      width: '6px', height: '6px', borderRadius: '50%',
      background: color, display: 'inline-block',
      boxShadow: `0 0 6px ${color}80`,
      animation: 'pulse 2s ease-in-out infinite',
    }} />
  );
}

const SkeletonRow = () => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '7px 0', borderBottom: '1px solid #f8fafc' }}>
    <div className="skeleton-shine" style={{ width: '58px', height: '10px', borderRadius: '3px' }} />
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '5px' }}>
      <div className="skeleton-shine" style={{ width: '46px', height: '11px', borderRadius: '3px' }} />
      <div className="skeleton-shine" style={{ width: '32px', height: '9px', borderRadius: '3px' }} />
    </div>
  </div>
);

const RowHover = ({ children, flashColor }: { children: React.ReactNode; flashColor?: string }) => {
  const [hover, setHover] = useState(false);
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        margin: '0 -8px', padding: '0 8px', borderRadius: '6px',
        background: flashColor ?? (hover ? '#faf9f6' : 'transparent'),
        transition: 'background .3s ease',
      }}
    >{children}</div>
  );
};

// ── Props kept for layout.tsx compatibility ────────────────────────────────
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export default function StockPanel(_props: { stocks?: Stock[]; stats?: QuickStat[] }) {
  const [data, setData] = useState<MarketData>(FALLBACK);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState('');
  const [flash, setFlash] = useState<Set<string>>(new Set());

  const fetchMarket = useCallback(async () => {
    try {
      const res = await fetch('/api/market', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: MarketData = await res.json();
      if (json.error) throw new Error(json.error);
      // Reject response if all prices are dashes (API ran but data source failed)
      const hasRealData = json.stocks?.some((s: {price: string}) => s.price && s.price !== '—');
      if (!hasRealData) throw new Error('No real price data');

      // Flash changed symbols
      setData(prev => {
        const changed = new Set<string>();
        for (const q of json.stocks) {
          const old = prev.stocks.find(s => s.symbol === q.symbol);
          if (old && old.price !== q.price) changed.add(q.symbol);
        }
        if (changed.size > 0) {
          setFlash(changed);
          setTimeout(() => setFlash(new Set()), 1200);
        }
        return json;
      });

      if (json.fetchedAt) {
        setLastUpdated(new Date(json.fetchedAt).toLocaleTimeString('en-IN', {
          hour: '2-digit', minute: '2-digit', second: '2-digit',
        }));
      }
    } catch (e) {
      console.warn('[StockPanel] fetch failed:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchMarket();
    // Refresh every 60s (API caches on server for 60s anyway)
    const timer = setInterval(fetchMarket, 60_000);
    return () => clearInterval(timer);
  }, [fetchMarket]);

  const indices = data.stocks.slice(0, 6);
  const equities = data.stocks.slice(6, 12);
  const gainers = data.gainers.length > 0 ? data.gainers : equities.filter(s => s.up).slice(0, 4);
  const losers  = data.losers.length  > 0 ? data.losers  : equities.filter(s => !s.up).slice(0, 3);

  // ── FII/DII: still static — no free public API for this ─────────────────
  const fiiNet = '+₹2,841 Cr';
  const diiNet = '+₹1,205 Cr';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>

      {/* ── Live Market ─────────────────────────────────────────────────── */}
      <Panel>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '4px' }}>
          <SectionLabel>Live Market</SectionLabel>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '2px' }}>
            <span style={{
              display: 'flex', alignItems: 'center', gap: '5px',
              fontSize: '9px', color: data.marketOpen ? '#15803d' : '#94a3b8',
              fontFamily: 'JetBrains Mono, monospace',
            }}>
              {loading ? (
                <span style={{ fontSize: '8px', color: '#cbd5e1' }}>loading…</span>
              ) : (
                <>
                  <Pulse color={data.marketOpen ? '#22c55e' : '#94a3b8'} />
                  {data.marketOpen ? 'OPEN' : 'CLOSED'}
                </>
              )}
            </span>
            {lastUpdated && (
              <span style={{ fontSize: '8px', fontFamily: 'JetBrains Mono, monospace',
                color: data.stale ? '#f59e0b' : (data.fromCache ? '#94a3b8' : '#22c55e') }}>
                {data.stale ? `⚠ stale · ${data.cacheAge ?? lastUpdated}` :
                 data.fromCache ? `⟳ cached · ${data.cacheAge ?? lastUpdated}` :
                 `↻ live · ${lastUpdated}`}
              </span>
            )}
          </div>
        </div>

        {/* Delay disclaimer */}
        <p style={{ fontSize: '8px', color: '#cbd5e1', fontFamily: 'JetBrains Mono, monospace', marginBottom: '8px', letterSpacing: '0.5px' }}>
          ⚡ NSE India · Live
        </p>

        {loading ? (
          Array.from({ length: 6 }).map((_, i) => <SkeletonRow key={i} />)
        ) : indices.map(s => (
          <RowHover key={s.symbol} flashColor={flash.has(s.symbol) ? (s.up ? '#f0fdf4' : '#fef2f2') : undefined}>
            <div style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '7px 0', borderBottom: '1px solid #f8fafc',
            }}>
              <span style={{
                fontSize: '10px', fontWeight: 600, color: '#475569',
                fontFamily: 'DM Sans, sans-serif', letterSpacing: '0.3px',
              }}>{s.symbol}</span>
              <div style={{ textAlign: 'right' }}>
                <p style={{
                  fontSize: '11px', color: '#0f172a',
                  fontFamily: 'JetBrains Mono, monospace', fontWeight: 500,
                }}>{s.price}</p>
                <p style={{
                  fontSize: '9px',
                  color: s.change === '—' ? '#94a3b8' : (s.up ? '#15803d' : '#b91c1c'),
                  fontFamily: 'JetBrains Mono, monospace',
                }}>{s.change === '—' ? '—' : `${s.up ? '▲' : '▼'} ${s.change}`}</p>
              </div>
            </div>
          </RowHover>
        ))}
      </Panel>

      {/* ── Top Gainers ─────────────────────────────────────────────────── */}
      <Panel>
        <SectionLabel>▲ Top Gainers</SectionLabel>
        {loading ? (
          Array.from({ length: 3 }).map((_, i) => (
            <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid #f8fafc' }}>
              <div className="skeleton-shine" style={{ width: '54px', height: '9px', borderRadius: '3px' }} />
              <div className="skeleton-shine" style={{ width: '36px', height: '9px', borderRadius: '3px' }} />
            </div>
          ))
        ) : gainers.length === 0 ? (
          <div style={{ fontSize: '10px', color: '#cbd5e1', fontFamily: 'DM Sans, sans-serif' }}>No data</div>
        ) : gainers.map(s => (
          <RowHover key={s.symbol}>
            <div style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '6px 0', borderBottom: '1px solid #f8fafc',
            }}>
              <span style={{ fontSize: '10px', color: '#64748b', fontFamily: 'DM Sans, sans-serif' }}>{s.symbol}</span>
              <span style={{ fontSize: '10px', color: '#15803d', fontFamily: 'JetBrains Mono, monospace', fontWeight: 600 }}>{s.change}</span>
            </div>
          </RowHover>
        ))}
      </Panel>

      {/* ── Top Losers ──────────────────────────────────────────────────── */}
      <Panel>
        <SectionLabel>▼ Top Losers</SectionLabel>
        {loading ? (
          Array.from({ length: 3 }).map((_, i) => (
            <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid #f8fafc' }}>
              <div className="skeleton-shine" style={{ width: '54px', height: '9px', borderRadius: '3px' }} />
              <div className="skeleton-shine" style={{ width: '36px', height: '9px', borderRadius: '3px' }} />
            </div>
          ))
        ) : losers.length === 0 ? (
          <div style={{ fontSize: '10px', color: '#cbd5e1', fontFamily: 'DM Sans, sans-serif' }}>No data</div>
        ) : losers.map(s => (
          <RowHover key={s.symbol}>
            <div style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '6px 0', borderBottom: '1px solid #f8fafc',
            }}>
              <span style={{ fontSize: '10px', color: '#64748b', fontFamily: 'DM Sans, sans-serif' }}>{s.symbol}</span>
              <span style={{ fontSize: '10px', color: '#b91c1c', fontFamily: 'JetBrains Mono, monospace', fontWeight: 600 }}>{s.change}</span>
            </div>
          </RowHover>
        ))}
      </Panel>

      {/* ── FII / DII ───────────────────────────────────────────────────── */}
      <Panel>
        <SectionLabel>FII / DII Activity</SectionLabel>
        <p style={{ fontSize: '8px', color: '#cbd5e1', fontFamily: 'JetBrains Mono, monospace', marginBottom: '8px' }}>
          ⚡ Indicative — previous session
        </p>
        <div style={{ display: 'flex', gap: '8px' }}>
          <div style={{
            flex: 1, background: '#f0fdf4', border: '1px solid #bbf7d0',
            borderRadius: '6px', padding: '10px', textAlign: 'center',
          }}>
            <p style={{ fontSize: '9px', color: '#64748b', fontFamily: 'DM Sans, sans-serif', letterSpacing: '1px', textTransform: 'uppercase' }}>FII Net</p>
            <p style={{ fontSize: '13px', color: '#15803d', fontFamily: 'JetBrains Mono, monospace', fontWeight: 600 }}>{fiiNet}</p>
          </div>
          <div style={{
            flex: 1, background: 'rgba(13,92,69,0.07)', border: '1px solid #bfdbfe',
            borderRadius: '6px', padding: '10px', textAlign: 'center',
          }}>
            <p style={{ fontSize: '9px', color: '#64748b', fontFamily: 'DM Sans, sans-serif', letterSpacing: '1px', textTransform: 'uppercase' }}>DII Net</p>
            <p style={{ fontSize: '13px', color: '#0d5c45', fontFamily: 'JetBrains Mono, monospace', fontWeight: 600 }}>{diiNet}</p>
          </div>
        </div>
      </Panel>

      {/* ── Global Indicators ───────────────────────────────────────────── */}
      <Panel>
        <SectionLabel>Global Indicators</SectionLabel>
        {loading ? (
          Array.from({ length: 5 }).map((_, i) => <SkeletonRow key={i} />)
        ) : data.stats.map(s => (
          <RowHover key={s.label}>
            <div style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '7px 0', borderBottom: '1px solid #f8fafc',
            }}>
              <span style={{ fontSize: '10px', color: '#64748b', fontFamily: 'DM Sans, sans-serif' }}>{s.label}</span>
              <div style={{ textAlign: 'right' }}>
                <p style={{ fontSize: '11px', color: '#0f172a', fontFamily: 'JetBrains Mono, monospace', fontWeight: 500 }}>{s.value}</p>
                <p style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>{s.sub}</p>
              </div>
            </div>
          </RowHover>
        ))}
      </Panel>

    </div>
  );
}
