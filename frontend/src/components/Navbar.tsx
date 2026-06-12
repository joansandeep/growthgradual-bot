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
      <nav style={{
        background: '#ffffff',
        borderBottom: '1px solid #e2e8f0',
        position: 'sticky',
        top: 0,
        zIndex: 100,
        boxShadow: '0 1px 4px rgba(0,0,0,0.06)',
      }}>
        {/* Top bar */}
        <div className="nav-top">
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <Image
              src="/growth-gradual-logo-transparent.jpeg"
              alt="Growth Gradual"
              width={180}
              height={52}
              style={{ objectFit: 'contain', height: '42px', width: 'auto' }}
              priority
            />
            <div style={{ width:'1px', height:'16px', background:'#e2e8f0' }} />
            <span className="nav-tagline" style={{ fontSize:'10px', color:'#94a3b8', fontFamily:"'DM Sans',sans-serif" }}>
              Indian Financial Markets · Live Data
            </span>
          </div>
          <div style={{ display:'flex', alignItems:'center', gap:'10px' }}>
            {marketOpen !== null && (
              <div style={{ display:'flex', alignItems:'center', gap:'5px', background: marketOpen ? '#f0fdf4' : '#f8fafc', border: `1px solid ${marketOpen ? '#bbf7d0' : '#e2e8f0'}`, borderRadius:'4px', padding:'3px 8px' }}>
                <span style={{ width:'6px', height:'6px', borderRadius:'50%', background: marketOpen ? '#22c55e' : '#94a3b8', display:'inline-block', boxShadow: marketOpen ? '0 0 6px #22c55e80' : 'none', animation: marketOpen ? 'pulse 2s ease-in-out infinite' : 'none' }} />
                <style>{`@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}`}</style>
                <span style={{ fontSize:'10px', color: marketOpen ? '#15803d' : '#64748b', fontFamily:'JetBrains Mono,monospace', letterSpacing:'0.5px' }}>{marketOpen ? 'MARKET OPEN' : 'MARKET CLOSED'}</span>
              </div>
            )}
            <span className="nav-tagline" style={{ fontSize:'10px', color:'#cbd5e1', fontFamily:'JetBrains Mono,monospace' }}>NSE · BSE · MCX</span>
          </div>
        </div>

        {/* Desktop nav links — hidden on home (chat) page */}
        <div className="nav-links" style={{ display: pathname === '/' ? 'none' : 'flex' }}>
          {CATEGORIES.map(cat => {
            const href = cat.id === 'all' ? '/' : `/${cat.id}`;
            const active = pathname === href || (cat.id === 'all' && pathname === '/');
            return (
              <Link key={cat.id} href={href} style={{
                display:'flex', alignItems:'center', gap:'5px',
                padding:'4px 12px', borderRadius:'2px',
                fontSize:'11px', fontWeight: active ? 600 : 400,
                fontFamily:"'DM Sans',sans-serif", letterSpacing:'0.8px',
                textTransform:'uppercase',
                color: active ? '#1e40af' : '#64748b',
                background: active ? '#eff6ff' : 'transparent',
                borderBottom: active ? '2px solid #3b82f6' : '2px solid transparent',
                textDecoration:'none', transition:'all 0.15s', whiteSpace:'nowrap',
              }}>
                <span style={{ fontSize:'9px', opacity:0.7 }}>{cat.icon}</span>
                {cat.label}
              </Link>
            );
          })}
          <div style={{ flex:1 }} />
          <span suppressHydrationWarning className="nav-tagline" style={{ fontSize:'10px', color:'#cbd5e1', fontFamily:'JetBrains Mono,monospace' }}>
            {time} IST
          </span>
        </div>
      </nav>

      {/* Mobile bottom nav — only visible on small screens */}
      <div className="mobile-bottom-nav">
        {CATEGORIES.map(cat => {
          const href = cat.id === 'all' ? '/' : `/${cat.id}`;
          const active = pathname === href || (cat.id === 'all' && pathname === '/');
          return (
            <Link key={cat.id} href={href} style={{
              display:'flex', flexDirection:'column', alignItems:'center', gap:'2px',
              padding:'4px 8px', textDecoration:'none', flex:1,
              color: active ? '#1e40af' : '#94a3b8',
            }}>
              <span style={{ fontSize:'16px' }}>{cat.icon}</span>
              <span style={{ fontSize:'8px', fontFamily:"'DM Sans',sans-serif", fontWeight: active ? 600 : 400, letterSpacing:'0.5px', textTransform:'uppercase' }}>
                {cat.label}
              </span>
            </Link>
          );
        })}
      </div>
    </>
  );
}
