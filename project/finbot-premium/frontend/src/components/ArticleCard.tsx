'use client';
import { useRouter } from 'next/navigation';
import { Article } from '@/types';
import { TAG_COLORS } from '@/data';

// Strip HTML tags that Google News sometimes puts in titles
function stripHtml(str: string): string {
  if (!str) return '';
  // First decode HTML entities
  let s = str
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#\d+;/g, '')
    .replace(/&[a-z]+;/g, '');
  // Remove anchor opening tags first so inner text is preserved
  s = s.replace(/<a[^>]*>/gi, '');
  // Strip remaining HTML tags
  s = s.replace(/<[^>]*>/g, '');
  // Strip any leftover partial/unclosed tags (e.g. "<a href=..." without closing >)
  s = s.replace(/<[^>]*$/, '');
  // If the whole string looks like a raw URL, return empty
  if (/^https?:\/\//.test(s.trim())) return '';
  return s.trim();
}

function buildArticleHref(article: Article): string {
  const params = new URLSearchParams({
    url:    article.url,
    title:  article.title,
    source: article.source,
    time:   article.time,
    tag:    article.tag,
  });
  if (article.image) params.set('image', article.image);
  if (article.source_url) params.set('sourceUrl', article.source_url);
  return `/article?${params.toString()}`;
}

export default function ArticleCard({
  article,
  featured = false,
  compact  = false,
}: {
  article:  Article;
  featured?: boolean;
  compact?:  boolean;
}) {
  const router   = useRouter();
  const tagColor = TAG_COLORS[article.tag] || '#dbeafe';
  const tagText  = TAG_TEXT[article.tag] || '#1e40af';
  const href     = buildArticleHref(article);

  const navigate = () => {
    if (article.url && article.url !== '#') {
      router.push(href);
    }
  };

  /* ── COMPACT LIST ROW ── */
  if (compact) {
    return (
      <div
        onClick={navigate}
        style={{
          padding: '10px 0',
          borderBottom: '1px solid #f1f5f9',
          cursor: 'pointer',
          transition: 'background 0.15s',
        }}
        onMouseEnter={e => (e.currentTarget.style.background = '#f8fafc')}
        onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
      >
        <span style={{
          display: 'inline-block', fontSize: '8px', fontWeight: 600,
          color: tagText, background: tagColor,
          padding: '2px 6px', borderRadius: '3px', marginBottom: '5px',
          fontFamily: 'DM Sans, sans-serif', letterSpacing: '0.8px', textTransform: 'uppercase',
        }}>{article.tag}</span>

        <p style={{
          fontSize: '12px', fontFamily: "'Playfair Display', serif",
          fontWeight: 600, color: '#334155', lineHeight: 1.45, marginBottom: '4px',
        }}>{stripHtml(article.title)}</p>

        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <span style={{ fontSize: '9px', color: '#64748b', fontFamily: 'DM Sans, sans-serif' }}>{article.source}</span>
          <span style={{ fontSize: '8px', color: '#cbd5e1' }}>·</span>
          <span style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>{article.time}</span>
          <span style={{ fontSize: '8px', color: '#cbd5e1' }}>·</span>
          <span style={{ fontSize: '9px', color: '#3b82f6', fontFamily: 'DM Sans, sans-serif' }}>Read →</span>
        </div>
      </div>
    );
  }

  /* ── FEATURED HERO ── */
  if (featured) {
    return (
      <div
        onClick={navigate}
        style={{
          background: '#ffffff', border: '1px solid #e2e8f0',
          borderRadius: '8px', overflow: 'hidden',
          cursor: 'pointer', transition: 'border-color 0.2s, box-shadow 0.2s',
          boxShadow: '0 1px 3px rgba(0,0,0,0.05)',
        }}
        onMouseEnter={e => {
          e.currentTarget.style.borderColor = '#93c5fd';
          e.currentTarget.style.boxShadow = '0 4px 12px rgba(59,130,246,0.1)';
        }}
        onMouseLeave={e => {
          e.currentTarget.style.borderColor = '#e2e8f0';
          e.currentTarget.style.boxShadow = '0 1px 3px rgba(0,0,0,0.05)';
        }}
      >
        {article.image && (
          <div style={{ width: '100%', height: '180px', overflow: 'hidden', borderBottom: '1px solid #e2e8f0' }}>
            <img
              src={article.image}
              alt=""
              onError={e => { (e.currentTarget.parentElement as HTMLElement).style.display = 'none'; }}
              style={{ width: '100%', height: '180px', objectFit: 'cover', display: 'block' }}
            />
          </div>
        )}
        <div style={{
          background: 'linear-gradient(135deg, #f0f9ff 0%, #eff6ff 60%, #f8fafc 100%)',
          borderBottom: '1px solid #e0e7ff',
          padding: '20px 20px 16px',
          position: 'relative',
        }}>
          <span style={{
            display: 'inline-block', fontSize: '8px', fontWeight: 700,
            color: tagText, background: tagColor,
            padding: '3px 8px', borderRadius: '3px', marginBottom: '10px',
            fontFamily: 'DM Sans, sans-serif', letterSpacing: '1px', textTransform: 'uppercase',
          }}>{article.tag}</span>

          <h2 style={{
            fontFamily: "'Playfair Display', serif",
            fontSize: 'clamp(15px, 2.5vw, 19px)', fontWeight: 700,
            color: '#0f172a', lineHeight: 1.3,
            marginBottom: article.summary ? '10px' : '0',
            letterSpacing: '-0.2px',
          }}>{stripHtml(article.title)}</h2>

          {article.summary && stripHtml(article.summary) && (
            <p style={{
              fontSize: '12px', color: '#64748b', lineHeight: 1.65,
              fontFamily: 'DM Sans, sans-serif',
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            } as React.CSSProperties}>{stripHtml(article.summary)}</p>
          )}
        </div>

        <div style={{
          padding: '10px 20px',
          display: 'flex', alignItems: 'center', gap: '10px',
          background: '#ffffff',
        }}>
          <div style={{
            width: '20px', height: '20px', borderRadius: '4px',
            background: '#eff6ff',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: '8px', color: '#3b82f6', fontWeight: 700,
            fontFamily: 'JetBrains Mono, monospace',
          }}>
            {article.source.slice(0, 2).toUpperCase()}
          </div>
          <span style={{ fontSize: '11px', color: '#475569', fontFamily: 'DM Sans, sans-serif', fontWeight: 500 }}>{article.source}</span>
          <span style={{ fontSize: '9px', color: '#cbd5e1' }}>·</span>
          <span style={{ fontSize: '10px', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>{article.time}</span>
          <div style={{ flex: 1 }} />
          <span style={{
            fontSize: '9px', color: '#3b82f6',
            fontFamily: 'DM Sans, sans-serif', letterSpacing: '0.5px', textTransform: 'uppercase',
          }}>Read →</span>
        </div>
      </div>
    );
  }

  /* ── GRID CARD ── */
  return (
    <div
      onClick={navigate}
      style={{
        background: '#ffffff', border: '1px solid #e2e8f0',
        borderRadius: '8px', overflow: 'hidden',
        cursor: 'pointer', height: '100%',
        transition: 'border-color 0.2s, box-shadow 0.2s',
        boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
        display: 'flex', flexDirection: 'column',
      }}
      onMouseEnter={e => {
        e.currentTarget.style.borderColor = '#93c5fd';
        e.currentTarget.style.boxShadow = '0 4px 12px rgba(59,130,246,0.08)';
      }}
      onMouseLeave={e => {
        e.currentTarget.style.borderColor = '#e2e8f0';
        e.currentTarget.style.boxShadow = '0 1px 3px rgba(0,0,0,0.04)';
      }}
    >
      {article.image && (
        <div style={{ width: '100%', height: '140px', overflow: 'hidden', borderBottom: '1px solid #e2e8f0', flexShrink: 0 }}>
          <img
            src={article.image}
            alt=""
            onError={e => { (e.currentTarget.parentElement as HTMLElement).style.display = 'none'; }}
            style={{ width: '100%', height: '140px', objectFit: 'cover', display: 'block' }}
          />
        </div>
      )}
      <div style={{ padding: '14px', flex: 1, display: 'flex', flexDirection: 'column' }}>
      <span style={{
        display: 'inline-block', fontSize: '8px', fontWeight: 700,
        color: tagText, background: tagColor,
        padding: '2px 6px', borderRadius: '3px', marginBottom: '8px',
        fontFamily: 'DM Sans, sans-serif', letterSpacing: '0.8px', textTransform: 'uppercase',
      }}>{article.tag}</span>

      <p style={{
        fontSize: '12px', fontFamily: "'Playfair Display', serif",
        fontWeight: 600, color: '#334155', lineHeight: 1.45,
        marginBottom: '10px',
        display: '-webkit-box',
        WebkitLineClamp: 3,
        WebkitBoxOrient: 'vertical',
        overflow: 'hidden',
      } as React.CSSProperties}>{stripHtml(article.title)}</p>

      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginTop: 'auto' }}>
        <span style={{ fontSize: '10px', color: '#64748b', fontFamily: 'DM Sans, sans-serif', fontWeight: 500 }}>{article.source}</span>
        <span style={{ fontSize: '8px', color: '#e2e8f0' }}>·</span>
        <span style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>{article.time}</span>
      </div>
      </div>
    </div>
  );
}

const TAG_TEXT: Record<string, string> = {
  Markets: '#1e40af', Economy: '#166534', Macro: '#166534',
  Stocks: '#1e40af', Global: '#3730a3', Banking: '#1e3a8a',
  Finance: '#1e40af', 'Mutual Funds': '#6b21a8', NAV: '#166534',
  Policy: '#7f1d1d', Regulatory: '#7c2d12', Analysis: '#713f12',
  Research: '#451a03', Results: '#14532d', Guide: '#1e3a5f',
  Rating: '#7f1d1d', Data: '#164e63', Technical: '#14532d',
  Picks: '#4c1d95', NFO: '#5b21b6',
};
