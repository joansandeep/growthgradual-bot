'use client';
import { useEffect, useState, Suspense } from 'react';
import { useSearchParams, useRouter } from 'next/navigation';
import { ArticleContent } from '@/app/api/article/route';
import { STOCKS } from '@/data';
import StockPanel from '@/components/StockPanel';
import MobileMarketBar from '@/components/MobileMarketBar';
import { QUICK_STATS } from '@/data';

function SkeletonLine({ w = '100%', h = 14 }: { w?: string; h?: number }) {
  return (
    <div className="skeleton" style={{
      width: w, height: `${h}px`,
      background: '#e2e8f0', borderRadius: '3px', marginBottom: '10px',
    }} />
  );
}

function ArticleReader() {
  const params = useSearchParams();
  const router = useRouter();
  const encodedUrl = params.get('url');
  const title     = params.get('title') || '';
  const source    = params.get('source') || '';
  const time      = params.get('time') || '';
  const tag       = params.get('tag') || '';
  const cardImage = params.get('image') || '';
  const sourceUrl = params.get('sourceUrl') || '';  // real publisher URL fallback
  const from      = params.get('from') || '';       // page the article was opened from

  const [content, setContent] = useState<ArticleContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState('');
  const [imgErrors, setImgErrors] = useState<Set<number>>(new Set());

  useEffect(() => {
    if (!encodedUrl) return;
    const articleUrl = decodeURIComponent(encodedUrl);
    setLoading(true);
    setError('');

    // Build API URL — pass sourceUrl as fallback so the route can scrape the
    // real publisher page when the primary URL is an unresolvable Google News proxy.
    const apiParams = new URLSearchParams({ url: articleUrl });
    if (sourceUrl) apiParams.set('sourceUrl', sourceUrl);

    fetch(`/api/article?${apiParams.toString()}`)
      .then(r => r.json())
      .then(data => {
        if (data.error) throw new Error(data.error);
        setContent(data);
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [encodedUrl, sourceUrl]);

  const displayTitle   = (content?.title && content.title !== 'Article') ? content.title : title;
  // Prefer the `source` URL param (set by the scraper to the real publisher name) when
  // content.source looks like a Google-owned domain (resolution failed) or is missing.
  const contentSourceIsGoogle = content?.source
    ? /google(?:usercontent|apis|news|\.com)/i.test(content.source)
    : false;
  const displaySource  = contentSourceIsGoogle ? (source || content?.source || '') : (content?.source || source);
  const displayAuthor  = content?.author;
  const displayDate    = content?.publishedAt;
  const readingTime    = content?.readingTime;

  const handleBackToFeed = () => {
    if (from === '/') {
      // Article was opened from the slide-in news panel on the chat page —
      // tell NewsFAB to reopen that panel once we land back on '/' instead
      // of leaving the user staring at the bare chatbot.
      try { sessionStorage.setItem('gg:reopen-news', '1'); } catch { /* ignore */ }
      router.push('/');
    } else if (from) {
      router.push(from);
    } else {
      router.back();
    }
  };

  return (
    <>
      <MobileMarketBar />
      <div className="page-grid">

      {/* LEFT: Stock Panel */}
      <div className="stock-panel-left">
        <StockPanel stocks={STOCKS} stats={QUICK_STATS} />
      </div>

      {/* CENTER: Article */}
      <div>
        {/* Back button */}
        <button
          onClick={handleBackToFeed}
          style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            background: 'none', border: 'none',
            color: '#64748b', cursor: 'pointer',
            fontSize: '11px', fontFamily: 'DM Sans, sans-serif',
            letterSpacing: '0.5px', marginBottom: '20px',
            padding: 0, transition: 'color 0.15s',
          }}
          onMouseEnter={e => (e.currentTarget.style.color = '#3b82f6')}
          onMouseLeave={e => (e.currentTarget.style.color = '#64748b')}
        >
          ← Back to Feed
        </button>

        <article style={{
          background: '#ffffff',
          border: '1px solid #1a1e28',
          borderRadius: '4px',
          overflow: 'hidden',
        }}>
          <div style={{ padding: '28px 32px' }}>
            {/* Tag + source row */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px' }}>
              {tag && (
                <span style={{
                  fontSize: '9px', fontWeight: 700,
                  color: '#e0c870', background: '#1e3a5f',
                  padding: '3px 8px', borderRadius: '2px',
                  fontFamily: 'DM Sans, sans-serif',
                  letterSpacing: '1px', textTransform: 'uppercase',
                }}>{tag}</span>
              )}
              <span style={{ fontSize: '10px', color: '#64748b', fontFamily: 'DM Sans, sans-serif' }}>
                {displaySource}
              </span>
              {time && (
                <>
                  <span style={{ color: '#cbd5e1', fontSize: '9px' }}>·</span>
                  <span style={{ fontSize: '10px', color: '#2a4050', fontFamily: 'JetBrains Mono, monospace' }}>{time}</span>
                </>
              )}
              {readingTime && (
                <>
                  <span style={{ color: '#cbd5e1', fontSize: '9px' }}>·</span>
                  <span style={{ fontSize: '10px', color: '#2a4050', fontFamily: 'DM Sans, sans-serif' }}>{readingTime} min read</span>
                </>
              )}
            </div>

            {/* Title */}
            {loading ? (
              <>
                <SkeletonLine h={28} w="95%" />
                <SkeletonLine h={28} w="70%" />
              </>
            ) : (
              <h1 style={{
                fontFamily: "'Playfair Display', serif",
                fontSize: '26px', fontWeight: 700,
                color: '#0f172a', lineHeight: 1.3,
                letterSpacing: '-0.3px', marginBottom: '16px',
              }}>{displayTitle}</h1>
            )}

            {/* Author + date */}
            {!loading && (displayAuthor || displayDate) && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: '10px',
                marginBottom: '22px', paddingBottom: '22px',
                borderBottom: '1px solid #1a1e28',
              }}>
                {displayAuthor && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
                    <div style={{
                      width: '26px', height: '26px', borderRadius: '50%',
                      background: 'linear-gradient(135deg, #1e40af, #3b82f6)',
                      border: '1px solid #2a3a5a',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: '10px', color: '#3b82f6', fontWeight: 700,
                      fontFamily: 'JetBrains Mono, monospace',
                    }}>
                      {displayAuthor.slice(0, 1).toUpperCase()}
                    </div>
                    <span style={{ fontSize: '12px', color: '#5a7090', fontFamily: 'DM Sans, sans-serif', fontWeight: 500 }}>
                      {displayAuthor}
                    </span>
                  </div>
                )}
                {displayDate && (
                  <span style={{ fontSize: '11px', color: '#2a4050', fontFamily: 'DM Sans, sans-serif' }}>
                    {displayDate}
                  </span>
                )}
              </div>
            )}

            {/* Gold divider */}
            <div style={{ height: '1px', background: 'linear-gradient(90deg, #c9a84c33, transparent)', marginBottom: '24px' }} />

            {/* Body */}
            {loading ? (
              <div>
                {Array.from({ length: 12 }).map((_, i) => (
                  <SkeletonLine key={i} w={i % 4 === 3 ? '60%' : '100%'} h={13} />
                ))}
              </div>
            ) : error ? (
              <div style={{
                background: '#f8fafc', border: '1px solid #e2e8f0',
                borderRadius: '4px', padding: '32px', textAlign: 'center',
              }}>
                <p style={{ fontSize: '13px', color: '#475569', fontFamily: "'Playfair Display', serif", marginBottom: '8px', fontStyle: 'italic' }}>
                  Unable to load article content
                </p>
                <p style={{ fontSize: '11px', color: '#94a3b8', fontFamily: 'DM Sans, sans-serif', marginBottom: '20px' }}>
                  {error}
                </p>
                <a
                  href={encodedUrl ? decodeURIComponent(encodedUrl) : '#'}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    display: 'inline-block',
                    padding: '9px 20px',
                    background: '#0f172a', border: '1px solid #334155',
                    borderRadius: '3px', color: '#93c5fd',
                    fontSize: '11px', fontFamily: 'DM Sans, sans-serif',
                    textDecoration: 'none', letterSpacing: '1px',
                    textTransform: 'uppercase',
                  }}
                >
                  Open on {displaySource} →
                </a>
              </div>
            ) : content?.paragraphs.length === 0 ? (
              (() => {
                const articleUrl = encodedUrl ? decodeURIComponent(encodedUrl) : '#';
                const isGoogleNews = articleUrl.includes('news.google.com');
                // Use sourceUrl (real publisher page) for the button link when available,
                // otherwise fall back to the Google News proxy URL.
                const openUrl = (sourceUrl && sourceUrl !== articleUrl) ? sourceUrl : articleUrl;
                // Real publisher name: use `source` param (now set to real publisher by the scraper),
                // falling back to content.source or displaySource.
                const publisherName = source || content?.source || displaySource || 'the publisher';
                // "Content sourced from" footer: show real publisher domain if we have sourceUrl
                let sourceDomain = 'news.google.com';
                try {
                  sourceDomain = sourceUrl ? new URL(sourceUrl).hostname.replace(/^www\./, '') : 'news.google.com';
                } catch { /* keep default */ }

                return (
                  <div style={{ textAlign: 'center', padding: '40px 24px' }}>
                    {isGoogleNews ? (
                      <>
                        <div style={{
                          width: '48px', height: '48px', borderRadius: '50%',
                          background: '#eff6ff', border: '1px solid #bfdbfe',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          margin: '0 auto 16px', fontSize: '20px',
                        }}>🗞</div>
                        <p style={{ fontSize: '14px', fontFamily: "'Playfair Display', serif", color: '#1e293b', marginBottom: '8px', fontWeight: 600 }}>
                          {title || 'Article from ' + publisherName}
                        </p>
                        <p style={{ fontSize: '11px', color: '#64748b', fontFamily: 'DM Sans, sans-serif', marginBottom: '6px' }}>
                          This article is hosted on <strong>{publisherName}</strong>.
                        </p>
                        <p style={{ fontSize: '11px', color: '#94a3b8', fontFamily: 'DM Sans, sans-serif', marginBottom: '24px' }}>
                          Click below to open the original article on {publisherName}.
                        </p>
                      </>
                    ) : (
                      <>
                        <p style={{ fontSize: '14px', color: '#94a3b8', fontFamily: "'Playfair Display', serif", fontStyle: 'italic', marginBottom: '8px' }}>
                          Content could not be extracted from this source.
                        </p>
                        <p style={{ fontSize: '11px', color: '#cbd5e1', fontFamily: 'DM Sans, sans-serif', marginBottom: '20px' }}>
                          This publisher may require JavaScript or a subscription.
                        </p>
                      </>
                    )}
                    <a
                      href={openUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{
                        display: 'inline-block',
                        padding: '10px 22px',
                        background: '#0f172a', border: '1px solid #334155',
                        borderRadius: '3px', color: '#93c5fd',
                        fontSize: '11px', fontFamily: 'DM Sans, sans-serif',
                        textDecoration: 'none', letterSpacing: '1px',
                        textTransform: 'uppercase',
                      }}
                    >
                      Open on {publisherName} →
                    </a>
                    <div style={{ marginTop: '16px', fontSize: '10px', color: '#cbd5e1', fontFamily: 'DM Sans, sans-serif' }}>
                      Content sourced from <strong style={{ color: '#94a3b8' }}>{isGoogleNews ? sourceDomain : content?.source || source}</strong>
                    </div>
                  </div>
                );
              })()
            ) : (
              <div>
                {/* Full-content fetch status */}
                {content && (
                  <div style={{
                    display: 'inline-flex', alignItems: 'center', gap: '6px',
                    marginBottom: '20px',
                    padding: '4px 10px', borderRadius: '3px',
                    background: content.fullContentFetched ? 'rgba(34,197,94,0.06)' : 'rgba(201,168,76,0.06)',
                    border: `1px solid ${content.fullContentFetched ? 'rgba(34,197,94,0.2)' : 'rgba(201,168,76,0.2)'}`,
                  }}>
                    <span style={{
                      width: '5px', height: '5px', borderRadius: '50%',
                      background: content.fullContentFetched ? '#22c55e' : '#3b82f6',
                      display: 'inline-block', flexShrink: 0,
                    }} />
                    <span style={{
                      fontSize: '9px', letterSpacing: '0.8px', textTransform: 'uppercase',
                      fontFamily: 'JetBrains Mono, monospace',
                      color: content.fullContentFetched ? '#22c55e' : '#3b82f6',
                    }}>
                      {content.fullContentFetched
                        ? `Full article · ${content.paragraphs.length} paragraphs · ${content.readingTime} min read`
                        : `Partial content · ${content.paragraphs.length} paragraphs extracted`}
                    </span>
                  </div>
                )}

                {/* Lead image at TOP: shown before body when it appears before text in the original article */}
                {content?.heroImagePosition === 'top' && (content?.images[0] || cardImage) && !imgErrors.has(-1) && (
                  <div style={{ margin: '0 0 24px', borderRadius: '4px', overflow: 'hidden', border: '1px solid #1a1e28' }}>
                    <img
                      src={content?.images[0] || cardImage}
                      alt={displayTitle}
                      onError={() => setImgErrors(s => new Set([...s, -1]))}
                      style={{ width: '100%', maxHeight: '380px', objectFit: 'cover', display: 'block' }}
                    />
                  </div>
                )}

                {(content?.paragraphs ?? []).map((para, i) => {
                  const isHeading = para.startsWith('§ ');
                  const text = isHeading ? para.slice(2) : para;
                  const images = content?.images ?? [];

                  // Lead image AFTER LEDE: shown after paragraph 0 when text comes first in original article
                  const showLeadImage = i === 0
                    && content?.heroImagePosition === 'afterLede'
                    && (images[0] || cardImage)
                    && !imgErrors.has(-1);

                  // Subsequent images (index 1+) every 5 paragraphs
                  const inlineImg = images[Math.floor(i / 5)];
                  const showImg = i > 0 && i % 5 === 0 && !!inlineImg && !imgErrors.has(Math.floor(i / 5));

                  return (
                    <div key={i}>
                      {isHeading ? (
                        <h3 style={{
                          fontFamily: "'Playfair Display', serif",
                          fontSize: '16px', fontWeight: 600,
                          color: '#1e293b', lineHeight: 1.4,
                          margin: '24px 0 10px',
                          paddingLeft: '12px',
                          borderLeft: '2px solid #3b82f644',
                        }}>{text}</h3>
                      ) : (
                        <p style={{
                          lineHeight: 1.9,
                          color: i === 0 ? '#475569' : '#64748b',
                          fontFamily: i === 0 ? "'Playfair Display', serif" : 'DM Sans, sans-serif',
                          fontSize: i === 0 ? '16px' : '14px',
                          fontWeight: i === 0 ? 400 : 300,
                          marginBottom: i === 0 ? '22px' : '15px',
                          letterSpacing: i === 0 ? '0.1px' : '0',
                        }}>{text}</p>
                      )}
                      {/* Lead image: shown after lede paragraph (i===0), mirrors original article layout */}
                      {showLeadImage && (
                        <div style={{ margin: '0 0 24px', borderRadius: '4px', overflow: 'hidden', border: '1px solid #1a1e28' }}>
                          <img
                            src={images[0] || cardImage}
                            alt={displayTitle}
                            onError={() => setImgErrors(s => new Set([...s, -1]))}
                            style={{ width: '100%', maxHeight: '380px', objectFit: 'cover', display: 'block' }}
                          />
                        </div>
                      )}
                      {showImg && (
                        <div style={{ margin: '24px 0', borderRadius: '4px', overflow: 'hidden', border: '1px solid #1a1e28' }}>
                          <img
                            src={inlineImg}
                            alt=""
                            onError={() => setImgErrors(s => new Set([...s, Math.floor(i / 5)]))}
                            style={{ width: '100%', maxHeight: '320px', objectFit: 'cover', display: 'block' }}
                          />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* Source attribution footer */}
            {!loading && !error && (
              <div style={{
                marginTop: '32px', paddingTop: '20px',
                borderTop: '1px solid #1a1e28',
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  {content?.favicon && (
                    <img src={content.favicon} alt="" width={16} height={16}
                      style={{ borderRadius: '2px' }}
                      onError={e => (e.currentTarget.style.display = 'none')} />
                  )}
                  <span style={{ fontSize: '11px', color: '#94a3b8', fontFamily: 'DM Sans, sans-serif' }}>
                    Content sourced from <strong style={{ color: '#2563eb' }}>{displaySource}</strong>
                  </span>
                </div>
                <a
                  href={encodedUrl ? decodeURIComponent(encodedUrl) : '#'}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    fontSize: '10px', color: '#3b82f6',
                    fontFamily: 'DM Sans, sans-serif',
                    textDecoration: 'none', letterSpacing: '0.8px',
                    textTransform: 'uppercase', opacity: 0.7,
                    transition: 'opacity 0.15s',
                  }}
                  onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
                  onMouseLeave={e => (e.currentTarget.style.opacity = '0.7')}
                >
                  View original →
                </a>
              </div>
            )}
          </div>
        </article>
      </div>

      {/* RIGHT: Related / TOC panel */}
      <div className="sidebar-right" style={{ position: 'sticky', top: '90px' }}>
        <div style={{
          background: '#ffffff', border: '1px solid #1a1e28',
          borderRadius: '4px', padding: '16px',
        }}>
          <p style={{
            fontSize: '9px', fontWeight: 600, color: '#64748b',
            textTransform: 'uppercase', letterSpacing: '2px',
            marginBottom: '12px', fontFamily: 'DM Sans, sans-serif',
            borderBottom: '1px solid #3b82f622', paddingBottom: '8px',
          }}>About This Article</p>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div>
              <p style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'DM Sans, sans-serif', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '3px' }}>Source</p>
              <p style={{ fontSize: '12px', color: '#3b82f6', fontFamily: 'DM Sans, sans-serif' }}>{displaySource || '—'}</p>
            </div>
            {displayDate && (
              <div>
                <p style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'DM Sans, sans-serif', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '3px' }}>Published</p>
                <p style={{ fontSize: '11px', color: '#64748b', fontFamily: 'DM Sans, sans-serif' }}>{displayDate}</p>
              </div>
            )}
            {displayAuthor && (
              <div>
                <p style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'DM Sans, sans-serif', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '3px' }}>Author</p>
                <p style={{ fontSize: '12px', color: '#3b82f6', fontFamily: 'DM Sans, sans-serif' }}>{displayAuthor}</p>
              </div>
            )}
            {readingTime && (
              <div>
                <p style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'DM Sans, sans-serif', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '3px' }}>Reading Time</p>
                <p style={{ fontSize: '12px', color: '#3b82f6', fontFamily: 'DM Sans, sans-serif' }}>{readingTime} minutes</p>
              </div>
            )}
          </div>

          {/* Image gallery */}
          {!loading && content && content.images.length > 1 && (
            <div style={{ marginTop: '18px' }}>
              <p style={{
                fontSize: '9px', fontWeight: 600, color: '#64748b',
                textTransform: 'uppercase', letterSpacing: '2px',
                marginBottom: '10px', fontFamily: 'DM Sans, sans-serif',
                borderBottom: '1px solid #3b82f622', paddingBottom: '8px',
              }}>Images</p>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px' }}>
                {content.images.slice(0, 6).map((img, i) => (
                  !imgErrors.has(i + 10) && (
                    <img
                      key={i}
                      src={img}
                      alt=""
                      onError={() => setImgErrors(s => new Set([...s, i + 10]))}
                      style={{
                        width: '100%', height: '60px', objectFit: 'cover',
                        borderRadius: '3px', border: '1px solid #1a1e28',
                        display: 'block',
                      }}
                    />
                  )
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
    </>
  );
}

export default function ArticlePage() {
  return (
    <Suspense fallback={
      <div style={{ color: '#64748b', padding: '40px', textAlign: 'center', fontFamily: 'DM Sans, sans-serif' }}>
        Loading article…
      </div>
    }>
      <ArticleReader />
    </Suspense>
  );
}
