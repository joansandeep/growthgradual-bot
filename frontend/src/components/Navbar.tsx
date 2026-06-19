'use client';
import Link from 'next/link';
import Image from 'next/image';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { CATEGORIES } from '@/data';

export default function Navbar() {
  const pathname = usePathname();
  const [time, setTime] = useState('');
  const [marketOpen, setMarketOpen] = useState<boolean | null>(null);

  useEffect(() => {
    const tick = () => setTime(new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }));
    tick();
    const t = setInterval(tick, 60_000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await fetch('/api/market', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        if (typeof data.marketOpen === 'boolean') setMarketOpen(data.marketOpen);
      } catch { /* keep null */ }
    };
    fetchStatus();
    const t = setInterval(fetchStatus, 60_000);
    return () => clearInterval(t);
  }, []);

  return (
    <>
      <style>{`
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.35}}
        @keyframes glow{0%,100%{box-shadow:0 0 6px rgba(34,197,94,0.4)}50%{box-shadow:0 0 14px rgba(34,197,94,0.7)}}
        .nav-link-item {
          display:flex;align-items:center;gap:5px;
          padding:5px 13px;border-radius:3px;
          font-size:10.5px;font-weight:500;
          font-family:'DM Sans',sans-serif;letter-spacing:0.7px;
          text-transform:uppercase;text-decoration:none;
          color:rgba(15,23,42,0.5);
          border-bottom:2px solid transparent;
          transition:color .15s,border-color .15s,background .15s;
          white-space:nowrap;
        }
        .nav-link-item:hover { color:rgba(15,23,42,0.85); background:rgba(15,23,42,0.04); }
        .nav-link-item.active {
          color:#0f172a;font-weight:700;
          border-bottom-color:#c8922a;
        }
        .mob-nav-item {
          display:flex;flex-direction:column;align-items:center;gap:3px;
          padding:4px 8px;text-decoration:none;flex:1;
          color:#94a3b8;transition:color .15s;
        }
        .mob-nav-item.active { color:#0d5c45; }
      `}</style>

      <nav style={{
        background: 'linear-gradient(135deg, #fefdf9 0%, #f9f6ef 50%, #faf8f3 100%)',
        position: 'sticky',
        top: 0,
        zIndex: 100,
        borderBottom: '1px solid rgba(200,146,42,0.18)',
        boxShadow: '0 2px 20px rgba(15,23,42,0.08), 0 1px 4px rgba(200,146,42,0.06)',
      }}>
        {/* Thin gold accent line at very top */}
        <div style={{
          height: '2px',
          background: 'linear-gradient(90deg, transparent 0%, #c8922a 30%, #e8a830 60%, #c8922a 80%, transparent 100%)',
          opacity: 0.7,
        }} />

        {/* Top row */}
        <div className="nav-top">
          {/* Logo + tagline */}
          <div style={{ display:'flex', alignItems:'center', gap:'14px' }}>
            <Image
              src="/growth-gradual-logo.png"
              alt="Growth Gradual"
              width={200}
              height={56}
              style={{ objectFit:'contain', height:'40px', width:'auto' }}
              priority
            />
            <div style={{ width:'1px', height:'18px', background:'rgba(15,23,42,0.12)' }} />
            <span className="nav-tagline" style={{
              fontSize:'9.5px', color:'rgba(15,23,42,0.38)',
              fontFamily:"'DM Sans',sans-serif", letterSpacing:'1.2px', textTransform:'uppercase',
            }}>
              Indian Financial Markets · Live Data
            </span>
          </div>

          {/* Right cluster */}
          <div style={{ display:'flex', alignItems:'center', gap:'12px' }}>
            {/* Market status pill */}
            {marketOpen !== null && (
              <div style={{
                display:'flex', alignItems:'center', gap:'6px',
                background: marketOpen ? 'rgba(34,197,94,0.08)' : 'rgba(15,23,42,0.05)',
                border: `1px solid ${marketOpen ? 'rgba(34,197,94,0.25)' : 'rgba(15,23,42,0.12)'}`,
                borderRadius:'20px', padding:'4px 12px',
              }}>
                <span style={{
                  width:'6px', height:'6px', borderRadius:'50%',
                  background: marketOpen ? '#22c55e' : '#94a3b8',
                  display:'inline-block',
                  animation: marketOpen ? 'glow 2s ease-in-out infinite' : 'none',
                }} />
                <span style={{
                  fontSize:'9px', fontFamily:'JetBrains Mono,monospace',
                  color: marketOpen ? '#16a34a' : 'rgba(15,23,42,0.35)',
                  letterSpacing:'0.8px', fontWeight:600,
                }}>
                  {marketOpen ? 'MARKET OPEN' : 'MARKET CLOSED'}
                </span>
              </div>
            )}
            <span className="nav-tagline" style={{
              fontSize:'9px', color:'rgba(15,23,42,0.22)',
              fontFamily:'JetBrains Mono,monospace', letterSpacing:'0.5px',
            }}>NSE · BSE · MCX</span>
          </div>
        </div>

        {/* Nav links row — hidden on chat page */}
        <div className="nav-links" style={{
          display: pathname === '/' ? 'none' : 'flex',
          borderTop: '1px solid rgba(200,146,42,0.1)',
        }}>
          {CATEGORIES.map(cat => {
            const href = cat.id === 'all' ? '/feed' : `/${cat.id}`;
            const active = pathname === href || (cat.id === 'all' && (pathname === '/feed'));
            return (
              <Link key={cat.id} href={href} className={`nav-link-item${active ? ' active' : ''}`}>
                <span style={{ fontSize:'10px' }}>{cat.icon}</span>
                {cat.label}
              </Link>
            );
          })}
          <div style={{ flex:1 }} />
          <span suppressHydrationWarning className="nav-tagline" style={{
            fontSize:'9px', color:'rgba(15,23,42,0.28)',
            fontFamily:'JetBrains Mono,monospace',
          }}>
            {time} IST
          </span>
        </div>
      </nav>

      {/* Mobile bottom nav */}
      <div className="mobile-bottom-nav">
        {CATEGORIES.map(cat => {
          const href = cat.id === 'all' ? '/feed' : `/${cat.id}`;
          const active = pathname === href;
          return (
            <Link key={cat.id} href={href} className={`mob-nav-item${active ? ' active' : ''}`}>
              <span style={{ fontSize:'18px' }}>{cat.icon}</span>
              <span style={{ fontSize:'8px', fontFamily:"'DM Sans',sans-serif", fontWeight: active ? 700 : 400, letterSpacing:'0.5px', textTransform:'uppercase' }}>
                {cat.label}
              </span>
            </Link>
          );
        })}
      </div>
    </>
  );
}
