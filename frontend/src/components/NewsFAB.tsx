'use client';

import { useState, useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import FeedPage from '@/components/FeedPage';

export default function NewsFAB() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const router = useRouter();
  const isChatPage = pathname === '/';

  // Allow other parts of the app (e.g. the chat "Market News" welcome card)
  // to open this same news panel without duplicating its UI.
  useEffect(() => {
    const openHandler = () => setOpen(true);
    window.addEventListener('gg:open-news', openHandler);
    return () => window.removeEventListener('gg:open-news', openHandler);
  }, []);

  // The slide-in news panel only makes sense on the chat page. If the user
  // navigates away (e.g. by tapping an article inside the panel), close it
  // so the FAB cleanly becomes a "back to chat" button instead.
  useEffect(() => {
    if (!isChatPage) setOpen(false);
  }, [isChatPage]);

  // If we're landing back on the chat page after "Back to Feed" was tapped
  // from an article that was opened out of this panel, reopen it instead of
  // leaving the user looking at the bare chatbot.
  useEffect(() => {
    if (!isChatPage) return;
    try {
      if (sessionStorage.getItem('gg:reopen-news')) {
        sessionStorage.removeItem('gg:reopen-news');
        setOpen(true);
      }
    } catch { /* ignore */ }
  }, [isChatPage]);

  const handleFabClick = () => {
    if (isChatPage) {
      setOpen(true);
    } else {
      router.push('/');
    }
  };

  return (
    <>
      <style>{`
        /* ── News FAB ─────────────────────────────────────────────────────── */
        .nfab {
          position: fixed;
          bottom: 28px;
          left: 28px;
          z-index: 9999;
          width: 62px;
          height: 62px;
          border-radius: 50%;
          border: none;
          background: #0f172a;
          box-shadow: 0 4px 20px rgba(15,23,42,.4), 0 0 0 3px rgba(15,23,42,.1);
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          flex-direction: column;
          gap: 2px;
          transition: transform .22s cubic-bezier(.34,1.56,.64,1), box-shadow .22s;
          overflow: hidden;
        }
        .nfab:hover {
          transform: scale(1.1);
          box-shadow: 0 8px 32px rgba(15,23,42,.5);
        }
        .nfab:active { transform: scale(.95); }
        .nfab__ring {
          position: absolute; inset: -7px; border-radius: 50%;
          border: 2px solid rgba(15,23,42,.3);
          animation: nfab-pulse 2.8s ease-out infinite;
          pointer-events: none;
        }
        @keyframes nfab-pulse {
          0%   { transform: scale(1);    opacity: .7; }
          70%  { transform: scale(1.28); opacity: 0; }
          100% { transform: scale(1.28); opacity: 0; }
        }
        .nfab__label {
          font-size: 8px;
          font-weight: 700;
          color: rgba(255,255,255,.75);
          letter-spacing: .08em;
          font-family: 'DM Sans', sans-serif;
          text-transform: uppercase;
          line-height: 1;
        }
        .nfab__dot {
          width: 5px; height: 5px; border-radius: 50%;
          background: #22c55e;
          box-shadow: 0 0 5px #22c55e;
          animation: nfab-blink 2s ease-in-out infinite;
          position: absolute; top: 10px; right: 10px;
        }
        @keyframes nfab-blink { 0%,100% { opacity:1; } 50% { opacity:.25; } }

        /* ── Backdrop ─────────────────────────────────────────────────────── */
        .nfab-backdrop {
          position: fixed; inset: 0; z-index: 9997;
          background: rgba(15,23,42,.3);
          backdrop-filter: blur(3px);
          animation: nfab-fi .2s ease;
        }
        @keyframes nfab-fi { from { opacity:0; } to { opacity:1; } }

        /* ── Slide panel ──────────────────────────────────────────────────── */
        .nfab-panel {
          position: fixed;
          left: 0; top: 0; bottom: 0;
          z-index: 9998;
          width: min(480px, 95vw);
          background: #f0f4f8;
          box-shadow: 4px 0 32px rgba(15,23,42,.2);
          display: flex;
          flex-direction: column;
          overflow: hidden;
          animation: nfab-slide .28s cubic-bezier(.34,1.56,.64,1);
        }
        @keyframes nfab-slide {
          from { transform: translateX(-100%); opacity: 0; }
          to   { transform: translateX(0);     opacity: 1; }
        }

        /* ── Panel header ─────────────────────────────────────────────────── */
        .nfab-panel__hdr {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 14px 16px 12px;
          background: #0f172a;
          flex-shrink: 0;
        }
        .nfab-panel__title {
          font-size: 14px;
          font-weight: 700;
          color: #fff;
          font-family: 'Playfair Display', serif;
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .nfab-panel__close {
          width: 30px; height: 30px; border-radius: 8px;
          border: 1px solid rgba(255,255,255,.2);
          background: rgba(255,255,255,.08);
          color: rgba(255,255,255,.8);
          cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          transition: background .15s;
        }
        .nfab-panel__close:hover { background: rgba(255,255,255,.18); }

        /* ── Scroll body ──────────────────────────────────────────────────── */
        .nfab-panel__body {
          flex: 1;
          overflow-y: auto;
          padding: 0;
          scrollbar-width: thin;
          scrollbar-color: #cbd5e1 transparent;
        }
        .nfab-panel__body::-webkit-scrollbar { width: 4px; }
        .nfab-panel__body::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }

        /* Override FeedPage layout inside panel — single column, no side panels */
        .nfab-panel__body .page-grid {
          display: block !important;
          padding: 0 !important;
        }
        .nfab-panel__body .stock-panel-left,
        .nfab-panel__body .sidebar-right {
          display: none !important;
        }
        .nfab-panel__body .main-container {
          padding: 12px 14px !important;
          max-width: 100% !important;
        }
      `}</style>

      {/* FAB button */}
      <button className="nfab" onClick={handleFabClick} aria-label={isChatPage ? 'Open latest news' : 'Back to chat'}>
        {!open && isChatPage && <span className="nfab__ring" />}
        <span className="nfab__dot" />
        {isChatPage ? (
          /* Newspaper icon */
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/>
            <path d="M18 14h-8M15 18h-5M10 6h8v4h-8V6Z"/>
          </svg>
        ) : (
          /* Chat bubble icon */
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
          </svg>
        )}
        <span className="nfab__label">{isChatPage ? 'News' : 'Chat'}</span>
      </button>

      {/* Backdrop */}
      {open && isChatPage && <div className="nfab-backdrop" onClick={() => setOpen(false)} />}

      {/* Slide-in panel */}
      {open && isChatPage && (
        <div className="nfab-panel" role="dialog" aria-label="Latest news">
          <div className="nfab-panel__hdr">
            <div className="nfab-panel__title">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/>
                <path d="M18 14h-8M15 18h-5M10 6h8v4h-8V6Z"/>
              </svg>
              Latest News
              <span style={{ fontSize: 9, fontWeight: 500, color: 'rgba(255,255,255,.5)', fontFamily: 'DM Sans,sans-serif', textTransform: 'uppercase', letterSpacing: '1px' }}>· Live Feed</span>
            </div>
            <button className="nfab-panel__close" onClick={() => setOpen(false)} aria-label="Close news">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <path d="M18 6L6 18M6 6l12 12"/>
              </svg>
            </button>
          </div>
          <div className="nfab-panel__body">
            <FeedPage category="all" />
          </div>
        </div>
      )}
    </>
  );
}
