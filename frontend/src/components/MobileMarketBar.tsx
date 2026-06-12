'use client';
import { useEffect, useState, useCallback } from 'react';

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
}

export default function MobileMarketBar() {
  const [data, setData] = useState<MarketData | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  const fetchMarket = useCallback(async () => {
    try {
      const res = await fetch('/api/market', { cache: 'no-store' });
      if (!res.ok) return;
      const json: MarketData = await res.json();
      const hasReal = json.stocks?.some(s => s.price && s.price !== '—');
      if (hasReal) setData(json);
    } catch { /* silent */ } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchMarket();
    const t = setInterval(fetchMarket, 60_000);
    return () => clearInterval(t);
  }, [fetchMarket]);

  const indices = data?.stocks.slice(0, 6) ?? [];
  const gainers = data?.gainers ?? [];
  const losers  = data?.losers  ?? [];
  const stats   = data?.stats   ?? [];
  const marketOpen = data?.marketOpen ?? false;

  return (
    <div className="mobile-market-bar">
      {/* ── Compact pill strip ── */}
      <div
        className="mobile-market-strip"
        onClick={() => setOpen(o => !o)}
        role="button"
        aria-expanded={open}
      >
        <div className="mobile-market-strip-inner">
          {/* Market status dot */}
          <span className="mmb-status-dot" style={{ background: marketOpen ? '#22c55e' : '#94a3b8', boxShadow: marketOpen ? '0 0 5px #22c55e80' : 'none' }} />
          <span className="mmb-status-label" style={{ color: marketOpen ? '#15803d' : '#64748b' }}>
            {marketOpen ? 'OPEN' : 'CLOSED'}
          </span>
          <span className="mmb-divider">│</span>

          {/* Scrollable quotes */}
          <div className="mmb-quotes-scroll">
            {loading ? (
              <span className="mmb-loading">Loading market data…</span>
            ) : indices.map(s => (
              <span key={s.symbol} className="mmb-quote">
                <span className="mmb-sym">{s.symbol}</span>
                <span className="mmb-price">{s.price}</span>
                <span className="mmb-chg" style={{ color: s.change === '—' ? '#94a3b8' : s.up ? '#15803d' : '#dc2626' }}>
                  {s.change === '—' ? '—' : `${s.up ? '▲' : '▼'} ${s.change}`}
                </span>
              </span>
            ))}
          </div>

          <span className="mmb-chevron" style={{ transform: open ? 'rotate(180deg)' : 'rotate(0deg)' }}>▾</span>
        </div>
      </div>

      {/* ── Expanded panel ── */}
      {open && (
        <div className="mmb-expanded">
          {/* Indices */}
          <div className="mmb-section">
            <div className="mmb-section-title">
              <span>📈 Indices</span>
              <span className="mmb-live-badge">⚡ NSE · Live</span>
            </div>
            <div className="mmb-grid">
              {indices.map(s => (
                <div key={s.symbol} className="mmb-card">
                  <span className="mmb-card-sym">{s.symbol}</span>
                  <span className="mmb-card-price">{s.price}</span>
                  <span className="mmb-card-chg" style={{ color: s.change === '—' ? '#94a3b8' : s.up ? '#15803d' : '#dc2626', background: s.change === '—' ? 'transparent' : s.up ? '#f0fdf4' : '#fef2f2' }}>
                    {s.change === '—' ? '—' : `${s.up ? '▲' : '▼'} ${s.change}`}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Gainers + Losers side by side */}
          {(gainers.length > 0 || losers.length > 0) && (
            <div className="mmb-gl-row">
              {gainers.length > 0 && (
                <div className="mmb-section mmb-section-half">
                  <div className="mmb-section-title">▲ Top Gainers</div>
                  {gainers.slice(0, 3).map(s => (
                    <div key={s.symbol} className="mmb-gl-item">
                      <span className="mmb-gl-sym">{s.symbol}</span>
                      <span className="mmb-gl-chg" style={{ color: '#15803d' }}>{s.change}</span>
                    </div>
                  ))}
                </div>
              )}
              {losers.length > 0 && (
                <div className="mmb-section mmb-section-half">
                  <div className="mmb-section-title">▼ Top Losers</div>
                  {losers.slice(0, 3).map(s => (
                    <div key={s.symbol} className="mmb-gl-item">
                      <span className="mmb-gl-sym">{s.symbol}</span>
                      <span className="mmb-gl-chg" style={{ color: '#dc2626' }}>{s.change}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Global stats */}
          {stats.length > 0 && (
            <div className="mmb-section">
              <div className="mmb-section-title">🌐 Global</div>
              <div className="mmb-stats-grid">
                {stats.map(s => (
                  <div key={s.label} className="mmb-stat-card">
                    <span className="mmb-stat-label">{s.label}</span>
                    <span className="mmb-stat-value">{s.value}</span>
                    <span className="mmb-stat-sub">{s.sub}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <button className="mmb-close" onClick={() => setOpen(false)}>✕ Close</button>
        </div>
      )}
    </div>
  );
}
