'use client';
import { useState, useEffect, useCallback } from 'react';
import { Category, Article } from '@/types';
import { STOCKS, QUICK_STATS, CATEGORIES } from '@/data';
import StockPanel from '@/components/StockPanel';
import ArticleCard from '@/components/ArticleCard';
import Sidebar from '@/components/Sidebar';

const RELATED_FALLBACK: Article[] = [
  { id: 'r1', category: 'stocks',       title: 'Top 5 Nifty 50 stocks analysts are bullish on', source: 'Equitymaster',    url: '#', time: '3h ago', tag: 'Picks',     summary: '' },
  { id: 'r2', category: 'stocks',       title: 'Understanding FII vs DII flows and their market impact', source: 'Zerodha Varsity', url: '#', time: '4h ago', tag: 'Education', summary: '' },
  { id: 'r3', category: 'stocks',       title: 'How to read Screener.in for fundamental analysis', source: 'Angel One', url: '#', time: '5h ago', tag: 'Guide', summary: '' },
  { id: 'r4', category: 'mutual_funds', title: 'ICICI Prudential launches new NFO — should you invest?', source: 'IIFL Finance', url: '#', time: '5h ago', tag: 'NFO', summary: '' },
  { id: 'r6', category: 'stocks',       title: 'Nifty Bank forms bullish engulfing on weekly charts', source: 'CNBCTV18', url: '#', time: '6h ago', tag: 'Technical', summary: '' },
  { id: 'r7', category: 'finance',      title: "CRISIL upgrades Tata Motors credit outlook to 'Stable'", source: 'CRISIL', url: '#', time: '7h ago', tag: 'Rating', summary: '' },
  { id: 'r8', category: 'mutual_funds', title: 'Guide to direct vs regular mutual fund plans for 2026', source: 'Groww Finance', url: '#', time: '8h ago', tag: 'Guide', summary: '' },
  { id: 'r9', category: 'stocks',       title: 'Sensex 52-week high: which sectors are driving the rally?', source: 'Koyfin', url: '#', time: '9h ago', tag: 'Analysis', summary: '' },
  { id: 'r10', category: 'finance',     title: 'MOSPI: CPI inflation cools to 4.1% in April 2026', source: 'MOSPI', url: '#', time: '10h ago', tag: 'Data', summary: '' },
];

function SkeletonCard({ h = 110 }: { h?: number }) {
  return (
    <div className="skeleton" style={{
      background: '#f1f5f9', border: '1px solid #e2e8f0',
      borderRadius: '8px', height: `${h}px`, overflow: 'hidden', padding: '16px',
    }}>
      <div style={{ width: '56px', height: '11px', background: '#e2e8f0', borderRadius: '3px', marginBottom: '6px' }} />
      <div style={{ width: '95%', height: '13px', background: '#e2e8f0', borderRadius: '3px', marginBottom: '6px' }} />
      <div style={{ width: '75%', height: '13px', background: '#e2e8f0', borderRadius: '3px', marginBottom: '6px' }} />
      <div style={{ width: '45%', height: '10px', background: '#f1f5f9', borderRadius: '3px', marginTop: '8px' }} />
    </div>
  );
}

interface FeedMeta {
  total: number;
  sources: string[];
  fromCache: boolean;
  fetchedAt: string;
}

export default function FeedPage({ category }: { category: Category }) {
  const [articles, setArticles] = useState<Article[]>([]);
  const [meta, setMeta] = useState<FeedMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const [isScraping, setIsScraping] = useState(false);

  // Strip HTML from titles and filter out articles with empty/URL-only titles
  const stripTitle = (t: string) => {
    if (!t) return '';
    let s = t.replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
    // Remove anchor opening tags (keep inner text) before stripping all tags
    s = s.replace(/<a[^>]*>/gi, '');
    s = s.replace(/<[^>]*>/g, '').replace(/<[^>]*$/, '');
    if (/^https?:\/\//.test(s.trim())) return '';
    return s.trim();
  };

  const mapArticles = (raw: Article[]) => raw.map((a: Article & { timeMs?: number }) => ({
    id: a.id, title: stripTitle(a.title), source: a.source, url: a.url,
    time: a.time, tag: a.tag, category: a.category, summary: a.summary,
    ...(a.image ? { image: a.image } : {}),
    ...(a.source_url ? { source_url: a.source_url } : {}),
  })).filter(a => a.title.length > 0);

  const loadArticles = useCallback(async (forceRefresh = false) => {
    if (forceRefresh) setRefreshing(true);
    else setLoading(true);
    setError('');
    setElapsed(0);

    try {
      // First try GET — returns instantly if cache exists
      const res = await fetch('/api/scrape', { method: forceRefresh ? 'POST' : 'GET' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (data.articles?.length > 0) {
        setArticles(mapArticles(data.articles));
        setMeta({ total: data.total, sources: data.sources ?? [], fromCache: data.fromCache, fetchedAt: data.fetchedAt });
        setLoading(false);
        setRefreshing(false);
        setIsScraping(false);
        return;
      }

      // No cache — scraping in progress, poll every 3s for partial results
      setIsScraping(true);
      const startTime = Date.now();
      const ticker = setInterval(() => setElapsed(Math.floor((Date.now() - startTime) / 1000)), 1000);

      const poll = async () => {
        try {
          const pr = await fetch('/api/scrape');
          if (pr.ok) {
            const pd = await pr.json();
            if (pd.articles?.length > 0) {
              setArticles(mapArticles(pd.articles));
              setMeta({ total: pd.total, sources: pd.sources ?? [], fromCache: pd.fromCache, fetchedAt: pd.fetchedAt });
            }
          }
        } catch { /**/ }
      };

      // Poll every 4 seconds to show partial results as they come in
      const pollTimer = setInterval(poll, 4000);

      // Stop polling after 3 minutes max
      setTimeout(() => {
        clearInterval(pollTimer);
        clearInterval(ticker);
        setIsScraping(false);
        setLoading(false);
        setRefreshing(false);
      }, 180_000);

      // Wait for initial response to complete
      await new Promise(resolve => {
        const check = setInterval(async () => {
          const r = await fetch('/api/scrape').catch(() => null);
          if (!r) return;
          const d = await r.json().catch(() => null);
          if (d?.articles?.length > 0) {
            clearInterval(check);
            clearInterval(pollTimer);
            clearInterval(ticker);
            setIsScraping(false);
            setLoading(false);
            setRefreshing(false);
            setArticles(mapArticles(d.articles));
            setMeta({ total: d.total, sources: d.sources ?? [], fromCache: d.fromCache, fetchedAt: d.fetchedAt });
            resolve(null);
          }
        }, 4000);
      });

    } catch (e) {
      setError(`Failed to load feed: ${e instanceof Error ? e.message : 'Unknown error'}`);
      setLoading(false);
      setRefreshing(false);
      setIsScraping(false);
    }
  }, []);

  useEffect(() => {
    loadArticles(false);
    const timer = setInterval(() => loadArticles(false), 6 * 60 * 60 * 1000);
    return () => clearInterval(timer);
  }, [loadArticles]);

  // ── Strict category keyword filters ───────────────────────────────────────
  const CATEGORY_KEYWORDS: Record<string, RegExp> = {
    stocks: /\b(stock|share|equity|nifty|sensex|bse|nse|ipo|listing|gmp|allotment|sebi|fii|dii|rally|correction|bull|bear|trade|trading|nifty\s*50|smallcap|midcap|largecap|bluechip|dividend|buyback|rights\s*issue|bonus\s*share|circuit|upper\s*circuit|lower\s*circuit|52.week|pe\s*ratio|eps|screener|ticker|portfolio|gain|gainer|loser|analyst|target\s*price|buy\s*call|sell\s*call|hold|outperform|underperform)\b/i,
    banks: /\b(bank|banking|lender|credit|loan|npa|nim|rbi|repo\s*rate|monetary\s*policy|hdfc\s*bank|icici\s*bank|sbi|axis\s*bank|kotak|yes\s*bank|idfc|federal\s*bank|deposit|savings|current\s*account|nbfc|microfinance|mfi|priority\s*sector|capital\s*adequacy|tier\s*1|tier\s*2|basel|liquidity|clrb|slr|crr|cd\s*ratio|net\s*interest|interest\s*income|advances|disbursement|asset\s*quality|provision)\b/i,
    mutual_funds: /\b(mutual\s*fund|sip|nav|amc|amfi|nfo|fund\s*house|scheme|folio|elss|liquid\s*fund|debt\s*fund|hybrid\s*fund|equity\s*fund|index\s*fund|etf|exchange\s*traded|large\s*cap\s*fund|mid\s*cap\s*fund|small\s*cap\s*fund|flexi\s*cap|multi\s*cap|sectoral\s*fund|thematic\s*fund|stp|swp|lump\s*sum|returns|cagr|xirr|sharpe|alpha|beta|aum|inflow|outflow|redemption|icici\s*prudential|hdfc\s*mutual|nippon|axis\s*mutual|sbi\s*mutual|kotak\s*mutual|dsp|uti\s*mutual|mirae|motilal\s*oswal\s*mutual|aditya\s*birla|groww|etmoney|kuvera|paytm\s*money)\b/i,
    finance: /\b(economy|gdp|inflation|cpi|wpi|fiscal|budget|tax|gst|rbi|monetary|interest\s*rate|repo|reverse\s*repo|forex|currency|rupee|dollar|exchange\s*rate|trade\s*deficit|current\s*account|balance\s*of\s*payment|fdi|fpi|capital\s*market|bond|yield|g-sec|treasury|debt|sovereign|imf|world\s*bank|oecd|sebi|irdai|irda|insurance|gold|crude|oil|commodity|real\s*estate|housing|msme|startup|unicorn|fintech|payment|upi|regulatory|policy|reform|subsidy|disinvestment|privatisation|public\s*sector)\b/i,
  };

  const isRelevant = (article: Article, cat: Category): boolean => {
    if (cat === 'all') return true;
    const pattern = CATEGORY_KEYWORDS[cat];
    if (!pattern) return true;
    const text = `${article.title} ${article.summary || ''} ${article.tag || ''}`;
    // Accept if: category matches OR title/summary matches keyword pattern
    return article.category === cat || article.category === 'all' || pattern.test(text);
  };

  const cat = CATEGORIES.find(c => c.id === category);
  const filtered = articles.filter(a => isRelevant(a, category));

  const featured = filtered[0];
  const grid     = filtered.slice(1, 5);
  const list     = filtered.slice(5);

  const fetchedLabel = meta?.fetchedAt
    ? new Date(meta.fetchedAt).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })
    : '';

  return (
    <div className="page-grid">

      {/* ── LEFT ── */}
      <div className="stock-panel-left"><StockPanel stocks={STOCKS} stats={QUICK_STATS} /></div>

      {/* ── CENTER ── */}
      <div>

        {/* Section header */}
        <div className="section-header">
          <div style={{ display: 'flex', alignItems: 'baseline', gap: '10px' }}>
            <h2 style={{
              fontFamily: "'Playfair Display', serif",
              fontSize: '17px', fontWeight: 700,
              color: '#0f172a', letterSpacing: '-0.3px',
            }}>
              {cat?.label || 'All Markets'}
            </h2>
            <span style={{
              fontSize: '9px', color: '#94a3b8',
              fontFamily: 'DM Sans, sans-serif',
              textTransform: 'uppercase', letterSpacing: '1.5px',
            }}>Latest News</span>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            {meta && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                background: meta.fromCache ? '#fefce8' : '#f0fdf4',
                border: `1px solid ${meta.fromCache ? '#fde68a' : '#bbf7d0'}`,
                borderRadius: '4px', padding: '3px 8px',
              }}>
                <span style={{
                  width: '5px', height: '5px', borderRadius: '50%',
                  background: meta.fromCache ? '#d97706' : '#22c55e',
                  display: 'inline-block',
                }} />
                <span style={{
                  fontSize: '9px', color: meta.fromCache ? '#92400e' : '#15803d',
                  fontFamily: 'JetBrains Mono, monospace',
                }}>
                  {meta.fromCache ? 'CACHED' : 'LIVE'} · {meta.total} articles · {meta.sources.length} sources
                </span>
                {fetchedLabel && (
                  <span style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>
                    · {fetchedLabel} IST
                  </span>
                )}
              </div>
            )}

            <button
              onClick={() => loadArticles(true)}
              disabled={refreshing}
              style={{
                background: refreshing ? '#f8fafc' : '#ffffff',
                border: '1px solid #e2e8f0',
                borderRadius: '4px',
                padding: '4px 10px',
                fontSize: '9px',
                color: refreshing ? '#94a3b8' : '#64748b',
                cursor: refreshing ? 'not-allowed' : 'pointer',
                fontFamily: 'DM Sans, sans-serif',
                letterSpacing: '1px', textTransform: 'uppercase',
                transition: 'all 0.15s',
              }}
              onMouseEnter={e => { if (!refreshing) { e.currentTarget.style.borderColor = '#93c5fd'; e.currentTarget.style.color = '#3b82f6'; }}}
              onMouseLeave={e => { e.currentTarget.style.borderColor = '#e2e8f0'; e.currentTarget.style.color = '#64748b'; }}
            >
              {refreshing ? '↻ Fetching…' : '↻ Refresh'}
            </button>
          </div>
        </div>

        {/* Rule */}
        <div style={{ height: '1px', background: 'linear-gradient(90deg, #3b82f644, #3b82f611, transparent)', marginBottom: '8px' }} />

        {/* Error banner */}
        {error && (
          <div style={{
            background: '#fef2f2', border: '1px solid #fecaca',
            borderRadius: '6px', padding: '12px 16px',
            marginBottom: '8px', fontSize: '12px', color: '#991b1b',
            fontFamily: 'DM Sans, sans-serif',
          }}>
            ⚠ {error}
            <button
              onClick={() => loadArticles(false)}
              style={{
                marginLeft: '12px', background: 'none', border: 'none',
                color: '#3b82f6', cursor: 'pointer', fontSize: '11px',
                fontFamily: 'DM Sans, sans-serif', textDecoration: 'underline',
              }}
            >Retry</button>
          </div>
        )}

        {/* Loading skeletons */}
        {loading ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {/* Progress banner shown while scraping */}
            {isScraping && (
              <div style={{
                background: '#f0f9ff', border: '1px solid #bae6fd',
                borderRadius: '8px', padding: '14px 16px', marginBottom: '4px',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '6px' }}>
                  <div style={{
                    width: '8px', height: '8px', borderRadius: '50%',
                    background: '#3b82f6', animation: 'pulse 1.2s infinite',
                  }} />
                  <span style={{ fontSize: '12px', fontFamily: 'DM Sans, sans-serif', color: '#1d4ed8', fontWeight: 600 }}>
                    Fetching news from 54 sources one by one…
                  </span>
                  <span style={{ fontSize: '11px', fontFamily: 'JetBrains Mono, monospace', color: '#64748b', marginLeft: 'auto' }}>
                    {elapsed}s elapsed
                  </span>
                </div>
                {/* Progress bar */}
                <div style={{ height: '4px', background: '#e0f2fe', borderRadius: '4px', overflow: 'hidden' }}>
                  <div style={{
                    height: '100%', borderRadius: '4px', background: '#3b82f6',
                    width: `${Math.min((elapsed / 90) * 100, 95)}%`,
                    transition: 'width 1s linear',
                  }} />
                </div>
                <p style={{ fontSize: '10px', color: '#64748b', fontFamily: 'DM Sans, sans-serif', marginTop: '6px' }}>
                  Articles will appear as sources finish loading. This takes ~60–90 seconds on first run.
                </p>
              </div>
            )}
            <SkeletonCard h={130} />
            <div className="article-grid-2">
              {[1,2,3,4].map(i => <SkeletonCard key={i} />)}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
              {[1,2,3].map(i => <SkeletonCard key={i} h={62} />)}
            </div>
          </div>
        ) : (
          <>
            {featured && (
              <div style={{ marginBottom: '6px' }} className="fade-up">
                <ArticleCard article={featured} featured />
              </div>
            )}

            {grid.length > 0 && (
              <div className="article-grid-2">
                {grid.map(a => (
                  <div key={a.id} className="fade-up">
                    <ArticleCard article={a} />
                  </div>
                ))}
              </div>
            )}

            {list.length > 0 && (
              <div style={{ height: '1px', background: '#f1f5f9', margin: '1px 0 2px' }} />
            )}

            <div>
              {list.map(a => (
                <div key={a.id} className="fade-up">
                  <ArticleCard article={a} compact />
                </div>
              ))}
            </div>

            {filtered.length === 0 && (
              <div style={{
                textAlign: 'center', padding: '60px 20px',
                color: '#94a3b8', fontFamily: "'Playfair Display', serif",
                fontSize: '15px', fontStyle: 'italic',
              }}>
                No articles in this category yet.
                <br />
                <span style={{ fontSize: '11px', fontFamily: 'DM Sans, sans-serif', color: '#cbd5e1' }}>
                  Feeds update every 5 minutes — try refreshing.
                </span>
              </div>
            )}
          </>
        )}
      </div>

      {/* ── RIGHT ── */}
      <div className="sidebar-right"><Sidebar related={articles.filter(a => a.url && a.url !== '#').slice(0, 8)} /></div>
    </div>
  );
}
