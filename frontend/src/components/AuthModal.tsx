'use client';

import { useState, FormEvent } from 'react';
import { useAuth } from '@/contexts/AuthContext';

interface AuthModalProps {
  onClose: () => void;
}

type Mode = 'login' | 'signup';

export default function AuthModal({ onClose }: AuthModalProps) {
  const { signIn, signUp } = useAuth();
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setInfo(null);
    setSubmitting(true);

    const result = mode === 'login'
      ? await signIn(email, password)
      : await signUp(email, password);

    setSubmitting(false);

    if (result.error) {
      setError(result.error);
      return;
    }

    if (mode === 'signup') {
      setInfo('Account created. Check your email to confirm, then log in.');
      setMode('login');
      return;
    }

    onClose();
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000,
        background: 'rgba(15,23,42,0.45)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: '16px',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: '100%', maxWidth: '380px',
          background: 'linear-gradient(135deg, #fefdf9 0%, #f9f6ef 50%, #faf8f3 100%)',
          borderRadius: '14px',
          border: '1px solid rgba(200,146,42,0.18)',
          boxShadow: '0 12px 40px rgba(15,23,42,0.25)',
          padding: '28px 26px',
          position: 'relative',
        }}
      >
        <div style={{
          fontFamily: "'DM Sans',sans-serif", fontSize: '18px', fontWeight: 700,
          color: '#0f172a', marginBottom: '4px',
        }}>
          {mode === 'login' ? 'Log in' : 'Create account'}
        </div>
        <div style={{
          fontSize: '11.5px', color: 'rgba(15,23,42,0.5)', marginBottom: '20px',
        }}>
          Growth Gradual · Indian Financial Markets
        </div>

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <input
            type="email"
            required
            autoComplete="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            style={inputStyle}
          />
          <input
            type="password"
            required
            minLength={6}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            style={inputStyle}
          />

          {error && (
            <div style={{ fontSize: '12px', color: '#dc2626', lineHeight: 1.4 }}>{error}</div>
          )}
          {info && (
            <div style={{ fontSize: '12px', color: '#16a34a', lineHeight: 1.4 }}>{info}</div>
          )}

          <button
            type="submit"
            disabled={submitting}
            style={{
              marginTop: '4px',
              background: submitting ? '#c8922a99' : '#c8922a',
              color: '#fff',
              border: 'none',
              borderRadius: '8px',
              padding: '10px 16px',
              fontSize: '13px',
              fontWeight: 700,
              fontFamily: "'DM Sans',sans-serif",
              letterSpacing: '0.3px',
              cursor: submitting ? 'default' : 'pointer',
            }}
          >
            {submitting ? 'Please wait…' : mode === 'login' ? 'Log in' : 'Sign up'}
          </button>
        </form>

        <div style={{ marginTop: '16px', fontSize: '12px', color: 'rgba(15,23,42,0.55)', textAlign: 'center' }}>
          {mode === 'login' ? (
            <>
              No account?{' '}
              <button type="button" onClick={() => { setMode('signup'); setError(null); setInfo(null); }} style={linkStyle}>
                Sign up
              </button>
            </>
          ) : (
            <>
              Already have an account?{' '}
              <button type="button" onClick={() => { setMode('login'); setError(null); setInfo(null); }} style={linkStyle}>
                Log in
              </button>
            </>
          )}
        </div>

        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          style={{
            position: 'absolute', top: '14px', right: '16px',
            background: 'transparent', border: 'none', cursor: 'pointer',
            fontSize: '16px', color: 'rgba(15,23,42,0.4)',
          }}
        >
          ✕
        </button>
      </div>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  padding: '10px 12px',
  borderRadius: '8px',
  border: '1px solid rgba(15,23,42,0.15)',
  fontSize: '13px',
  fontFamily: "'DM Sans',sans-serif",
  outline: 'none',
  background: '#fff',
  color: '#0f172a',
};

const linkStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  color: '#c8922a',
  fontWeight: 700,
  cursor: 'pointer',
  padding: 0,
  fontSize: '12px',
};
