'use client';
import { useState, useEffect, useCallback } from 'react';
import { Category, Article } from '@/types';
import { STOCKS, QUICK_STATS, CATEGORIES } from '@/data';
import StockPanel from '@/components/StockPanel';
import MobileMarketBar from '@/components/MobileMarketBar';
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
    <div style={{
      background: '#ffffff', border: '1px solid #e6e2d8',
      borderRadius: '10px', height: `${h}px`, overflow: 'hidden', padding: '16px',
      boxShadow: '0 1px 4px rgba(15,23,42,0.04)',
    }}>
      <div className="skeleton-shine" style={{ width: '52px', height: '10px', borderRadius: '3px', marginBottom: '8px' }} />
      <div className="skeleton-shine" style={{ width: '92%', height: '13px', borderRadius: '3px', marginBottom: '6px' }} />
      <div className="skeleton-shine" style={{ width: '70%', height: '13px', borderRadius: '3px', marginBottom: '6px' }} />
      <div className="skeleton-shine" style={{ width: '40%', height: '10px', borderRadius: '3px', marginTop: '10px' }} />
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
        // Log source count to console only — not shown in UI
        console.log(`[Feed] ${data.sources?.length ?? 0} sources · ${data.total} articles · ${data.fromCache ? 'cached' : 'live'}`);
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
    <>
      <MobileMarketBar />
      <div className="page-grid">

      {/* ── LEFT ── */}
      <div className="stock-panel-left"><StockPanel stocks={STOCKS} stats={QUICK_STATS} /></div>

      {/* ── CENTER ── */}
      <div>
        {/* Section header */}
        <div className="section-header" style={{ marginBottom:'14px' }}>
          <div style={{ display:'flex', alignItems:'center', gap:'12px' }}>
            <div style={{ width:'3px', height:'20px', background:'linear-gradient(180deg,#0d5c45,#c8922a)', borderRadius:'2px' }}/>
            <div>
              <h2 style={{
                fontFamily:"'Playfair Display',serif",
                fontSize:'18px', fontWeight:800, color:'#0f172a', letterSpacing:'-0.4px', lineHeight:1.1,
              }}>{cat?.label || 'All Markets'}</h2>
              <span style={{ fontSize:'9px', color:'#94a3b8', fontFamily:"'DM Sans',sans-serif", textTransform:'uppercase', letterSpacing:'1.5px' }}>Latest News</span>
            </div>
          </div>

          <div style={{ display:'flex', alignItems:'center', gap:'10px' }}>
            {meta && (
              <div style={{
                display:'flex', alignItems:'center', gap:'5px',
                background: meta.fromCache ? 'rgba(200,146,42,0.08)' : 'rgba(13,92,69,0.07)',
                border:`1px solid ${meta.fromCache ? 'rgba(200,146,42,0.3)' : 'rgba(13,92,69,0.25)'}`,
                borderRadius:'20px', padding:'3px 10px',
              }}>
                <span style={{
                  width:'5px', height:'5px', borderRadius:'50%',
                  background: meta.fromCache ? '#c8922a' : '#0d5c45',
                  display:'inline-block',
                }}/>
                <span style={{ fontSize:'9px', color: meta.fromCache ? '#92400e' : '#0d5c45', fontFamily:'JetBrains Mono,monospace', fontWeight:600 }}>
                  {meta.fromCache ? 'CACHED' : 'LIVE'} · {meta.total}
                </span>
                {fetchedLabel && (
                  <span className="fetched-time" style={{ fontSize:'9px', color:'#94a3b8', fontFamily:'JetBrains Mono,monospace' }}>
                    · {fetchedLabel} IST
                  </span>
                )}
              </div>
            )}
            <button
              onClick={() => loadArticles(true)}
              disabled={refreshing}
              style={{
                background: refreshing ? 'rgba(13,92,69,0.05)' : '#fff',
                border:'1px solid #e6e2d8', borderRadius:'20px',
                padding:'4px 12px', fontSize:'9px',
                color: refreshing ? '#94a3b8' : '#0d5c45',
                cursor: refreshing ? 'not-allowed' : 'pointer',
                fontFamily:"'DM Sans',sans-serif", letterSpacing:'0.8px', textTransform:'uppercase',
                fontWeight:600, transition:'all .15s',
              }}
              onMouseEnter={e => { if (!refreshing) { e.currentTarget.style.borderColor='#0d5c45'; e.currentTarget.style.background='rgba(13,92,69,0.05)'; }}}
              onMouseLeave={e => { e.currentTarget.style.borderColor='#e4e8ef'; e.currentTarget.style.background='#fff'; }}
            >
              {refreshing ? '↻ Fetching…' : '↻ Refresh'}
            </button>
          </div>
        </div>

        {/* Premium rule */}
        <div className="premium-rule"/>

        {/* Error banner */}
        {error && (
          <div style={{
            background:'#fef2f2', border:'1px solid rgba(239,68,68,0.25)',
            borderRadius:'8px', padding:'12px 16px', marginBottom:'12px',
            fontSize:'12px', color:'#991b1b', fontFamily:"'DM Sans',sans-serif",
          }}>
            ⚠ {error}
            <button onClick={() => loadArticles(false)} style={{ marginLeft:'12px', background:'none', border:'none', color:'#0d5c45', cursor:'pointer', fontSize:'11px', fontFamily:"'DM Sans',sans-serif", textDecoration:'underline' }}>Retry</button>
          </div>
        )}

        {/* Loading state — only show full skeletons when there's truly no data yet.
            Once we have any articles (even partial, from polling), keep showing
            them and let the in-progress banner below indicate a refresh is happening. */}
        {loading && articles.length === 0 ? (
          <div style={{ display:'flex', flexDirection:'column', gap:'10px' }}>
            {isScraping && (
              <div style={{
                background:'linear-gradient(135deg,rgba(13,92,69,0.06),rgba(26,31,78,0.04))',
                border:'1px solid rgba(13,92,69,0.2)', borderRadius:'10px', padding:'16px 18px', marginBottom:'4px',
              }}>
                <div style={{ display:'flex', alignItems:'center', gap:'10px', marginBottom:'8px' }}>
                  <div style={{ width:'8px', height:'8px', borderRadius:'50%', background:'#0d5c45', animation:'pulse 1.2s infinite' }}/>
                  <span style={{ fontSize:'12px', fontFamily:"'DM Sans',sans-serif", color:'#0d5c45', fontWeight:700 }}>
                    Fetching from 54 sources…
                  </span>
                  <span style={{ fontSize:'11px', fontFamily:'JetBrains Mono,monospace', color:'#94a3b8', marginLeft:'auto' }}>
                    {elapsed}s
                  </span>
                </div>
                <div style={{ height:'3px', background:'rgba(13,92,69,0.12)', borderRadius:'3px', overflow:'hidden' }}>
                  <div style={{
                    height:'100%', borderRadius:'3px',
                    background:'linear-gradient(90deg,#0d5c45,#c8922a)',
                    width:`${Math.min((elapsed/90)*100,95)}%`, transition:'width 1s linear',
                  }}/>
                </div>
                <p style={{ fontSize:'10px', color:'#94a3b8', fontFamily:"'DM Sans',sans-serif", marginTop:'6px' }}>
                  Articles appear as sources load — takes ~60–90s on first run.
                </p>
              </div>
            )}
            <SkeletonCard h={140}/>
            <div className="article-grid-2">{[1,2,3,4].map(i=><SkeletonCard key={i}/>)}</div>
            <div style={{ display:'flex', flexDirection:'column', gap:'2px' }}>{[1,2,3].map(i=><SkeletonCard key={i} h={64}/>)}</div>
          </div>
        ) : (
          <>
            {(isScraping || refreshing) && articles.length > 0 && (
              <div style={{
                display:'flex', alignItems:'center', gap:'8px',
                background:'linear-gradient(135deg,rgba(13,92,69,0.06),rgba(26,31,78,0.04))',
                border:'1px solid rgba(13,92,69,0.2)', borderRadius:'8px',
                padding:'8px 14px', marginBottom:'10px',
              }}>
                <div style={{ width:'6px', height:'6px', borderRadius:'50%', background:'#0d5c45', animation:'pulse 1.2s infinite' }}/>
                <span style={{ fontSize:'11px', fontFamily:"'DM Sans',sans-serif", color:'#0d5c45', fontWeight:600 }}>
                  Updating feed… showing latest available while new articles load.
                </span>
              </div>
            )}
            {featured && (
              <div style={{ marginBottom:'10px' }} className="fade-up">
                <ArticleCard article={featured} featured/>
              </div>
            )}
            {grid.length > 0 && (
              <div className="article-grid-2">
                {grid.map(a=>(
                  <div key={a.id} className="fade-up">
                    <ArticleCard article={a}/>
                  </div>
                ))}
              </div>
            )}
            {list.length > 0 && (
              <div style={{
                background:'#fff', borderRadius:'10px', border:'1px solid #e6e2d8',
                boxShadow:'0 1px 6px rgba(15,23,42,0.04)',
                padding:'4px 0', marginTop:'4px',
              }}>
                <div style={{ padding:'10px 14px 6px', borderBottom:'1px solid #f0f2f7' }}>
                  <span style={{ fontSize:'9px', fontFamily:"'DM Sans',sans-serif", fontWeight:700, color:'#94a3b8', letterSpacing:'1.2px', textTransform:'uppercase' }}>More Stories</span>
                </div>
                {list.map(a=>(
                  <div key={a.id} className="fade-up">
                    <ArticleCard article={a} compact/>
                  </div>
                ))}
              </div>
            )}
            {filtered.length === 0 && (
              <div style={{ textAlign:'center', padding:'64px 20px', color:'#94a3b8', fontFamily:"'Playfair Display',serif", fontSize:'15px', fontStyle:'italic' }}>
                No articles in this category yet.
                <br/>
                <span style={{ fontSize:'11px', fontFamily:"'DM Sans',sans-serif", color:'#cbd5e1' }}>Feeds refresh every 5 minutes.</span>
              </div>
            )}
          </>
        )}
      </div>

      {/* ── RIGHT ── */}
      <div className="sidebar-right"><Sidebar related={articles.filter(a=>a.url&&a.url!=='#').slice(0,8)}/></div>
    </div>
    </>
  );
}
