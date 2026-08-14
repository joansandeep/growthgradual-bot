'use client';

import { useState, FormEvent, ReactNode } from 'react';
import { useAuth } from '@/contexts/AuthContext';

type Mode = 'login' | 'signup';

export default function AuthGate({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: '80px 20px', color: 'rgba(15,23,42,0.4)', fontFamily: "'DM Sans',sans-serif", fontSize: '13px' }}>
        Loading…
      </div>
    );
  }

  if (!user) {
    return <AuthScreen />;
  }

  return <>{children}</>;
}

function AuthScreen() {
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
    }
    // On login success, AuthContext's onAuthStateChange updates `user` and
    // AuthGate re-renders into the app automatically.
  };

  return (
    <div style={{
      display: 'flex', justifyContent: 'center', alignItems: 'center',
      minHeight: '70vh', padding: '40px 16px',
    }}>
      <div style={{
        width: '100%', maxWidth: '380px',
        background: 'linear-gradient(135deg, #fefdf9 0%, #f9f6ef 50%, #faf8f3 100%)',
        borderRadius: '14px',
        border: '1px solid rgba(200,146,42,0.18)',
        boxShadow: '0 8px 32px rgba(15,23,42,0.1)',
        padding: '32px 28px',
      }}>
        <div style={{
          fontFamily: "'DM Sans',sans-serif", fontSize: '20px', fontWeight: 700,
          color: '#0f172a', marginBottom: '4px', textAlign: 'center',
        }}>
          {mode === 'login' ? 'Log in' : 'Create account'}
        </div>
        <div style={{
          fontSize: '11.5px', color: 'rgba(15,23,42,0.5)', marginBottom: '22px', textAlign: 'center',
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
              padding: '11px 16px',
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

        <div style={{ marginTop: '18px', fontSize: '12px', color: 'rgba(15,23,42,0.55)', textAlign: 'center' }}>
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
