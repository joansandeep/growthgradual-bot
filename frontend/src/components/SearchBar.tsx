'use client';
import { useState } from 'react';

export default function SearchBar() {
  const [q, setQ] = useState('');
  const [answer, setAnswer] = useState('');
  const [loading, setLoading] = useState(false);
  const [asked, setAsked] = useState('');

  const search = async () => {
    if (!q.trim()) return;
    setAsked(q);
    setLoading(true);
    setAnswer('');
    try {
      const res = await fetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      });
      const data = await res.json();
      setAnswer(data.answer || 'No response.');
    } catch {
      setAnswer('Unable to reach Growing Gradual AI. Check your API key in .env.local');
    }
    setLoading(false);
  };

  return (
    <div style={{ marginBottom: '20px' }}>
      <div style={{ position: 'relative', display: 'flex', gap: '8px' }}>
        <div style={{ position: 'relative', flex: 1 }}>
          <span style={{
            position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)',
            fontSize: '11px', color: '#94a3b8',
          }}>⌕</span>
          <input
            value={q}
            onChange={e => setQ(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && search()}
            placeholder="Ask Growing Gradual — markets, stocks, funds, policy…"
            style={{
              width: '100%',
              padding: '10px 14px 10px 28px',
              fontSize: '12px',
              background: '#ffffff',
              border: '1px solid #e2e8f0',
              borderRadius: '6px',
              color: '#0f172a',
              fontFamily: 'DM Sans, sans-serif',
              outline: 'none',
              transition: 'border-color 0.2s, box-shadow 0.2s',
              boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
            }}
            onFocus={e => {
              e.target.style.borderColor = '#93c5fd';
              e.target.style.boxShadow = '0 0 0 3px rgba(59,130,246,0.1)';
            }}
            onBlur={e => {
              e.target.style.borderColor = '#e2e8f0';
              e.target.style.boxShadow = '0 1px 2px rgba(0,0,0,0.04)';
            }}
          />
        </div>
        <button
          onClick={search}
          style={{
            padding: '0 18px',
            background: '#1e40af',
            border: 'none',
            borderRadius: '6px',
            color: '#ffffff',
            fontSize: '11px',
            fontFamily: 'DM Sans, sans-serif',
            fontWeight: 600,
            letterSpacing: '1px',
            textTransform: 'uppercase',
            cursor: 'pointer',
            transition: 'background 0.15s',
            whiteSpace: 'nowrap',
          }}
          onMouseEnter={e => { e.currentTarget.style.background = '#1d4ed8'; }}
          onMouseLeave={e => { e.currentTarget.style.background = '#1e40af'; }}
        >
          Ask AI
        </button>
      </div>

      {/* AI Response */}
      {(loading || answer) && (
        <div style={{
          marginTop: '10px',
          background: '#f0f9ff',
          border: '1px solid #bae6fd',
          borderRadius: '6px',
          padding: '16px',
          borderLeft: '3px solid #3b82f6',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
            <div style={{
              width: '18px', height: '18px', borderRadius: '4px',
              background: 'linear-gradient(135deg, #3b82f6, #1e40af)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: '9px', color: '#ffffff', fontWeight: 700,
            }}>✦</div>
            <span style={{
              fontSize: '10px', fontWeight: 600,
              color: '#1e40af',
              fontFamily: 'DM Sans, sans-serif',
              letterSpacing: '1px',
              textTransform: 'uppercase',
            }}>Growing Gradual · &ldquo;{asked}&rdquo;</span>
          </div>
          {loading ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              {[0, 1, 2].map(i => (
                <span key={i} style={{
                  width: '5px', height: '5px', borderRadius: '50%',
                  background: '#3b82f6',
                  display: 'inline-block',
                  animation: `dotPulse 1.2s ${i * 0.2}s ease-in-out infinite`,
                }} />
              ))}
              <style>{`@keyframes dotPulse{0%,100%{opacity:0.2;transform:translateY(0)}50%{opacity:1;transform:translateY(-3px)}}`}</style>
              <span style={{ fontSize: '11px', color: '#64748b', fontFamily: 'DM Sans, sans-serif', marginLeft: '4px' }}>
                Querying financial knowledge base…
              </span>
            </div>
          ) : (
            <p style={{
              fontSize: '12px',
              color: '#334155',
              lineHeight: 1.7,
              whiteSpace: 'pre-wrap',
              fontFamily: 'DM Sans, sans-serif',
            }}>{answer}</p>
          )}
        </div>
      )}
    </div>
  );
}
