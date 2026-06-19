'use client';
import { useRouter } from 'next/navigation';
import { Article } from '@/types';
import { TAG_COLORS } from '@/data';

function stripHtml(str: string): string {
  if (!str) return '';
  let s = str
    .replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>')
    .replace(/&quot;/g,'"').replace(/&#39;/g,"'")
    .replace(/&#\d+;/g,'').replace(/&[a-z]+;/g,'');
  s = s.replace(/<a[^>]*>/gi,'');
  s = s.replace(/<[^>]*>/g,'').replace(/<[^>]*$/, '');
  if (/^https?:\/\//.test(s.trim())) return '';
  return s.trim();
}

function buildArticleHref(article: Article): string {
  const params = new URLSearchParams({ url:article.url, title:article.title, source:article.source, time:article.time, tag:article.tag });
  if (article.image) params.set('image', article.image);
  if (article.source_url) params.set('sourceUrl', article.source_url);
  return `/article?${params.toString()}`;
}

const TAG_TEXT: Record<string, string> = {
  Markets:'#0d5c45', Economy:'#0d5c45', Macro:'#0d5c45',
  Stocks:'#1a1f4e', Global:'#1a1f4e', Banking:'#1a1f4e',
  Finance:'#0d5c45', 'Mutual Funds':'#5b21b6', NAV:'#0d5c45',
  Policy:'#7f1d1d', Regulatory:'#7c2d12', Analysis:'#c8922a',
  Research:'#7c4a00', Results:'#14532d', Guide:'#1a1f4e',
  Rating:'#7f1d1d', Data:'#0d5c45', Technical:'#14532d',
  Picks:'#4c1d95', NFO:'#5b21b6',
};

const TAG_BG: Record<string, string> = {
  Markets:'rgba(13,92,69,0.1)', Economy:'rgba(13,92,69,0.1)', Macro:'rgba(13,92,69,0.1)',
  Stocks:'rgba(26,31,78,0.1)', Global:'rgba(26,31,78,0.1)', Banking:'rgba(26,31,78,0.1)',
  Finance:'rgba(13,92,69,0.1)', 'Mutual Funds':'rgba(91,33,182,0.1)', NAV:'rgba(13,92,69,0.1)',
  Policy:'rgba(127,29,29,0.1)', Regulatory:'rgba(124,45,18,0.1)', Analysis:'rgba(200,146,42,0.1)',
  Research:'rgba(124,74,0,0.1)', Results:'rgba(20,83,45,0.1)', Guide:'rgba(26,31,78,0.1)',
  Rating:'rgba(127,29,29,0.1)', Data:'rgba(13,92,69,0.1)', Technical:'rgba(20,83,45,0.1)',
  Picks:'rgba(76,29,149,0.1)', NFO:'rgba(91,33,182,0.1)',
};

export default function ArticleCard({ article, featured=false, compact=false }: {
  article: Article; featured?: boolean; compact?: boolean;
}) {
  const router = useRouter();
  const tagBg   = TAG_BG[article.tag]   ?? 'rgba(26,31,78,0.08)';
  const tagText = TAG_TEXT[article.tag] ?? '#1a1f4e';
  const href    = buildArticleHref(article);
  void TAG_COLORS; // imported but used via TAG_BG override

  const navigate = () => {
    if (article.url && article.url !== '#') router.push(href);
  };

  // Shared tag pill
  const TagPill = () => (
    <span style={{
      display:'inline-block', fontSize:'8px', fontWeight:700,
      color:tagText, background:tagBg,
      padding:'3px 8px', borderRadius:'3px', marginBottom:'9px',
      fontFamily:"'DM Sans',sans-serif", letterSpacing:'1px', textTransform:'uppercase',
      border:`1px solid ${tagText}22`,
    }}>{article.tag}</span>
  );

  /* ── COMPACT LIST ROW ── */
  if (compact) return (
    <div
      onClick={navigate}
      style={{
        padding:'11px 14px',
        borderBottom:'1px solid #eef0f5',
        cursor:'pointer',
        borderRadius:'6px',
        transition:'background .15s',
        display:'flex', gap:'12px', alignItems:'flex-start',
      }}
      onMouseEnter={e => {
        e.currentTarget.style.background='#f7f8fa';
        e.currentTarget.style.borderLeftColor='#0d5c45';
      }}
      onMouseLeave={e => {
        e.currentTarget.style.background='transparent';
        e.currentTarget.style.borderLeftColor='transparent';
      }}
    >
      <div style={{ flex:1, minWidth:0 }}>
        <TagPill />
        <p style={{
          fontSize:'12.5px', fontFamily:"'Playfair Display',serif",
          fontWeight:600, color:'#1a2035', lineHeight:1.42, marginBottom:'6px',
        }}>{stripHtml(article.title)}</p>
        <div style={{ display:'flex', gap:'6px', alignItems:'center' }}>
          <span style={{ fontSize:'9px', color:'#64748b', fontFamily:"'DM Sans',sans-serif", fontWeight:500 }}>{article.source}</span>
          <span style={{ color:'#d1d9e6', fontSize:'8px' }}>·</span>
          <span style={{ fontSize:'9px', color:'#94a3b8', fontFamily:'JetBrains Mono,monospace' }}>{article.time}</span>
          <span style={{ marginLeft:'auto', fontSize:'9px', color:'#0d5c45', fontFamily:"'DM Sans',sans-serif", fontWeight:600, letterSpacing:'0.3px' }}>Read →</span>
        </div>
      </div>
    </div>
  );

  /* ── FEATURED HERO ── */
  if (featured) return (
    <div
      onClick={navigate}
      style={{
        background:'#fff', borderRadius:'12px', overflow:'hidden',
        cursor:'pointer', border:'1px solid #e4e8ef',
        boxShadow:'0 2px 12px rgba(15,23,42,0.06)',
        transition:'box-shadow .2s,transform .2s',
      }}
      onMouseEnter={e => {
        e.currentTarget.style.boxShadow='0 8px 32px rgba(13,92,69,0.12)';
        e.currentTarget.style.transform='translateY(-2px)';
      }}
      onMouseLeave={e => {
        e.currentTarget.style.boxShadow='0 2px 12px rgba(15,23,42,0.06)';
        e.currentTarget.style.transform='translateY(0)';
      }}
    >
      {article.image && (
        <div style={{ width:'100%', height:'190px', overflow:'hidden' }}>
          <img src={article.image} alt="" onError={e => {(e.currentTarget.parentElement as HTMLElement).style.display='none';}}
            style={{ width:'100%', height:'190px', objectFit:'cover', display:'block' }} />
        </div>
      )}
      <div style={{
        background:'linear-gradient(135deg,rgba(13,92,69,0.04) 0%,rgba(26,31,78,0.03) 100%)',
        borderBottom:'1px solid #eef0f5',
        padding:'22px 22px 16px',
      }}>
        {/* Teal accent bar */}
        <div style={{ width:'32px', height:'3px', background:'linear-gradient(90deg,#0d5c45,#c8922a)', borderRadius:'2px', marginBottom:'14px' }}/>
        <TagPill />
        <h2 style={{
          fontFamily:"'Playfair Display',serif",
          fontSize:'clamp(16px,2.5vw,20px)', fontWeight:800,
          color:'#0f172a', lineHeight:1.28, marginBottom: article.summary ? '10px' : 0,
          letterSpacing:'-0.3px',
        }}>{stripHtml(article.title)}</h2>
        {article.summary && stripHtml(article.summary) && (
          <p style={{
            fontSize:'12.5px', color:'#64748b', lineHeight:1.65,
            fontFamily:"'DM Sans',sans-serif",
            display:'-webkit-box', WebkitLineClamp:2, WebkitBoxOrient:'vertical', overflow:'hidden',
          } as React.CSSProperties}>{stripHtml(article.summary)}</p>
        )}
      </div>
      <div style={{ padding:'12px 22px', display:'flex', alignItems:'center', gap:'10px', background:'#fff' }}>
        <div style={{
          width:'22px', height:'22px', borderRadius:'5px',
          background:'linear-gradient(135deg,#0d5c45,#1a1f4e)',
          display:'flex', alignItems:'center', justifyContent:'center',
          fontSize:'7px', color:'#fff', fontWeight:800, fontFamily:'JetBrains Mono,monospace',
        }}>
          {article.source.slice(0,2).toUpperCase()}
        </div>
        <span style={{ fontSize:'11.5px', color:'#334155', fontFamily:"'DM Sans',sans-serif", fontWeight:600 }}>{article.source}</span>
        <span style={{ color:'#d1d9e6', fontSize:'8px' }}>·</span>
        <span style={{ fontSize:'9.5px', color:'#94a3b8', fontFamily:'JetBrains Mono,monospace' }}>{article.time}</span>
        <div style={{ flex:1 }}/>
        <span style={{ fontSize:'9px', color:'#0d5c45', fontFamily:"'DM Sans',sans-serif", fontWeight:700, letterSpacing:'0.5px', textTransform:'uppercase' }}>Read →</span>
      </div>
    </div>
  );

  /* ── GRID CARD ── */
  return (
    <div
      onClick={navigate}
      style={{
        background:'#fff', borderRadius:'10px', overflow:'hidden',
        cursor:'pointer', height:'100%',
        border:'1px solid #e4e8ef',
        boxShadow:'0 1px 6px rgba(15,23,42,0.05)',
        display:'flex', flexDirection:'column',
        transition:'box-shadow .18s,transform .18s,border-color .18s',
      }}
      onMouseEnter={e => {
        e.currentTarget.style.boxShadow='0 6px 24px rgba(13,92,69,0.1)';
        e.currentTarget.style.borderColor='rgba(13,92,69,0.25)';
        e.currentTarget.style.transform='translateY(-1px)';
      }}
      onMouseLeave={e => {
        e.currentTarget.style.boxShadow='0 1px 6px rgba(15,23,42,0.05)';
        e.currentTarget.style.borderColor='#e6e2d8';
        e.currentTarget.style.transform='translateY(0)';
      }}
    >
      {article.image && (
        <div style={{ width:'100%', height:'145px', overflow:'hidden', flexShrink:0 }}>
          <img src={article.image} alt="" onError={e => {(e.currentTarget.parentElement as HTMLElement).style.display='none';}}
            style={{ width:'100%', height:'145px', objectFit:'cover', display:'block', transition:'transform .3s' }}
            onMouseEnter={e => (e.currentTarget.style.transform='scale(1.03)')}
            onMouseLeave={e => (e.currentTarget.style.transform='scale(1)')}
          />
        </div>
      )}
      <div style={{ padding:'14px 15px', flex:1, display:'flex', flexDirection:'column' }}>
        <TagPill />
        <p style={{
          fontSize:'12.5px', fontFamily:"'Playfair Display',serif",
          fontWeight:700, color:'#1a2035', lineHeight:1.42, marginBottom:'10px',
          display:'-webkit-box', WebkitLineClamp:3, WebkitBoxOrient:'vertical', overflow:'hidden',
        } as React.CSSProperties}>{stripHtml(article.title)}</p>
        <div style={{ display:'flex', alignItems:'center', gap:'6px', marginTop:'auto' }}>
          <span style={{ fontSize:'10px', color:'#64748b', fontFamily:"'DM Sans',sans-serif", fontWeight:500 }}>{article.source}</span>
          <span style={{ color:'#d1d9e6', fontSize:'8px' }}>·</span>
          <span style={{ fontSize:'9px', color:'#94a3b8', fontFamily:'JetBrains Mono,monospace' }}>{article.time}</span>
        </div>
      </div>
    </div>
  );
}
