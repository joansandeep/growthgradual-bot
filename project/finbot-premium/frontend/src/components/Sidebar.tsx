'use client';
import { useRouter } from 'next/navigation';
import { Article } from '@/types';
import { TAG_COLORS } from '@/data';

const TAG_TEXT: Record<string, string> = {
  Markets: '#1e40af', Economy: '#166534', Macro: '#166534',
  Stocks: '#1e40af', Global: '#3730a3', Banking: '#1e3a8a',
  Finance: '#1e40af', 'Mutual Funds': '#6b21a8', NAV: '#166534',
  Policy: '#7f1d1d', Regulatory: '#7c2d12', Analysis: '#713f12',
  Research: '#451a03', Results: '#14532d', Guide: '#1e3a5f',
  Rating: '#7f1d1d', Data: '#164e63', Technical: '#14532d',
  Picks: '#4c1d95', NFO: '#5b21b6',
};

function buildHref(a: Article): string {
  const params = new URLSearchParams({ url: a.url, title: a.title, source: a.source, time: a.time, tag: a.tag });
  return `/article?${params.toString()}`;
}

export default function Sidebar({ related }: { related: Article[] }) {
  const router = useRouter();

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>

      <div style={{ background: '#ffffff', border: '1px solid #e2e8f0', borderRadius: '8px', padding: '10px 12px', boxShadow: '0 1px 3px rgba(0,0,0,0.04)' }}>
        <p style={{
          fontSize: '9px', fontWeight: 600, color: '#94a3b8',
          textTransform: 'uppercase', letterSpacing: '2px',
          marginBottom: '8px', fontFamily: 'DM Sans, sans-serif',
          borderBottom: '1px solid #f1f5f9', paddingBottom: '8px',
        }}>Further Reading</p>

        {related.slice(0, 6).map((a, i) => (
          <div
            key={a.id}
            onClick={() => a.url && a.url !== '#' && router.push(buildHref(a))}
            style={{
              padding: '9px 0',
              borderBottom: i < 5 ? '1px solid #f8fafc' : 'none',
              cursor: a.url && a.url !== '#' ? 'pointer' : 'default',
              transition: 'padding-left 0.15s',
            }}
            onMouseEnter={e => { if (a.url && a.url !== '#') e.currentTarget.style.paddingLeft = '4px'; }}
            onMouseLeave={e => (e.currentTarget.style.paddingLeft = '0')}
          >
            <span style={{
              display: 'inline-block', fontSize: '8px', fontWeight: 600,
              color: TAG_TEXT[a.tag] || '#1e40af', background: TAG_COLORS[a.tag] || '#dbeafe',
              padding: '2px 5px', borderRadius: '3px', marginBottom: '5px',
              fontFamily: 'DM Sans, sans-serif', letterSpacing: '0.8px', textTransform: 'uppercase',
            }}>{a.tag}</span>
            <p style={{
              fontSize: '11px', fontFamily: "'Playfair Display', serif",
              fontWeight: 600, color: '#334155', lineHeight: 1.4, marginBottom: '4px',
            }}>{a.title}</p>
            <div style={{ display: 'flex', gap: '6px' }}>
              <span style={{ fontSize: '9px', color: '#64748b', fontFamily: 'DM Sans, sans-serif' }}>{a.source}</span>
              <span style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>{a.time}</span>
            </div>
          </div>
        ))}
      </div>

      <div style={{ background: '#ffffff', border: '1px solid #e2e8f0', borderRadius: '8px', padding: '10px 12px', boxShadow: '0 1px 3px rgba(0,0,0,0.04)' }}>
        <p style={{
          fontSize: '9px', fontWeight: 600, color: '#94a3b8',
          textTransform: 'uppercase', letterSpacing: '2px',
          marginBottom: '8px', fontFamily: 'DM Sans, sans-serif',
          borderBottom: '1px solid #f1f5f9', paddingBottom: '8px',
        }}>Active Sources</p>
        {['Moneycontrol', 'Economic Times', 'Livemint', 'NDTV Profit', 'Business Standard', 'Reuters India', 'Financial Express'].map(src => (
          <div key={src} style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '5px 0', borderBottom: '1px solid #f8fafc',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: '#22c55e', display: 'inline-block' }} />
              <span style={{ fontSize: '10px', color: '#475569', fontFamily: 'DM Sans, sans-serif' }}>{src}</span>
            </div>
            <span style={{ fontSize: '9px', color: '#15803d', fontFamily: 'JetBrains Mono, monospace' }}>Live</span>
          </div>
        ))}
      </div>
    </div>
  );
}
