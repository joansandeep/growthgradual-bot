'use client';
import { useRouter } from 'next/navigation';
import { Article } from '@/types';

const TAG_TEXT: Record<string, string> = {
  Markets:'#0d5c45', Economy:'#0d5c45', Macro:'#0d5c45',
  Stocks:'#1a1f4e', Global:'#1a1f4e', Banking:'#1a1f4e',
  Finance:'#0d5c45', 'Mutual Funds':'#5b21b6', NAV:'#0d5c45',
  Policy:'#7f1d1d', Regulatory:'#7c2d12', Analysis:'#c8922a',
  Research:'#7c4a00', Results:'#14532d', Guide:'#1a1f4e',
  Rating:'#7f1d1d', Data:'#0d5c45', Technical:'#14532d',
  Picks:'#4c1d95', NFO:'#5b21b6',
};

function buildHref(a: Article): string {
  const params = new URLSearchParams({ url:a.url, title:a.title, source:a.source, time:a.time, tag:a.tag });
  return `/article?${params.toString()}`;
}

const SidePanel = ({ children, title }: { children: React.ReactNode; title: string }) => (
  <div style={{
    background:'#fff', border:'1px solid #e4e8ef', borderRadius:'10px',
    padding:'12px 14px', boxShadow:'0 2px 10px rgba(15,23,42,0.06)',
  }}>
    <div style={{ display:'flex', alignItems:'center', gap:'6px', marginBottom:'10px', paddingBottom:'8px', borderBottom:'1px solid #f0f2f7' }}>
      <div style={{ width:'2px', height:'12px', background:'linear-gradient(180deg,#0d5c45,#c8922a)', borderRadius:'2px' }}/>
      <p style={{ fontSize:'9px', fontWeight:700, color:'#8892a4', textTransform:'uppercase', letterSpacing:'2px', fontFamily:"'DM Sans',sans-serif", margin:0 }}>{title}</p>
    </div>
    {children}
  </div>
);

export default function Sidebar({ related }: { related: Article[] }) {
  const router = useRouter();

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:'10px' }}>

      <SidePanel title="Further Reading">
        {related.slice(0,6).map((a, i) => (
          <div
            key={a.id}
            onClick={() => a.url && a.url !== '#' && router.push(buildHref(a))}
            style={{
              padding:'9px 0',
              borderBottom: i < 5 ? '1px solid #f4f5f8' : 'none',
              cursor: a.url && a.url !== '#' ? 'pointer' : 'default',
              transition:'padding-left .15s',
            }}
            onMouseEnter={e => { if (a.url && a.url !== '#') e.currentTarget.style.paddingLeft='5px'; }}
            onMouseLeave={e => { e.currentTarget.style.paddingLeft='0'; }}
          >
            <span style={{
              display:'inline-block', fontSize:'7.5px', fontWeight:700,
              color: TAG_TEXT[a.tag] ?? '#0d5c45',
              background:`${TAG_TEXT[a.tag] ?? '#0d5c45'}15`,
              padding:'2px 6px', borderRadius:'3px', marginBottom:'5px',
              fontFamily:"'DM Sans',sans-serif", letterSpacing:'0.8px', textTransform:'uppercase',
            }}>{a.tag}</span>
            <p style={{
              fontSize:'11.5px', fontFamily:"'Playfair Display',serif",
              fontWeight:700, color:'#1a2035', lineHeight:1.38, marginBottom:'4px',
            }}>{a.title}</p>
            <div style={{ display:'flex', gap:'6px', alignItems:'center' }}>
              <span style={{ fontSize:'9px', color:'#64748b', fontFamily:"'DM Sans',sans-serif", fontWeight:500 }}>{a.source}</span>
              <span style={{ color:'#d1d9e6', fontSize:'8px' }}>·</span>
              <span style={{ fontSize:'8.5px', color:'#94a3b8', fontFamily:'JetBrains Mono,monospace' }}>{a.time}</span>
            </div>
          </div>
        ))}
      </SidePanel>

      <SidePanel title="Active Sources">
        {['Moneycontrol','Economic Times','Livemint','NDTV Profit','Business Standard','Reuters India','Financial Express'].map((src,i) => (
          <div key={src} style={{
            display:'flex', justifyContent:'space-between', alignItems:'center',
            padding:'6px 0', borderBottom: i < 6 ? '1px solid #f4f5f8' : 'none',
          }}>
            <div style={{ display:'flex', alignItems:'center', gap:'7px' }}>
              <span style={{ width:'5px', height:'5px', borderRadius:'50%', background:'#22c55e', display:'inline-block', boxShadow:'0 0 4px rgba(34,197,94,0.5)' }}/>
              <span style={{ fontSize:'10.5px', color:'#334155', fontFamily:"'DM Sans',sans-serif", fontWeight:500 }}>{src}</span>
            </div>
            <span style={{ fontSize:'8.5px', color:'#0d5c45', fontFamily:'JetBrains Mono,monospace', fontWeight:600, letterSpacing:'0.5px' }}>LIVE</span>
          </div>
        ))}
      </SidePanel>

      {/* Brand card */}
      <div style={{
        background:'linear-gradient(135deg,#0d5c45,#0f172a)',
        border:'none', borderRadius:'10px', padding:'16px',
        boxShadow:'0 4px 18px rgba(13,92,69,0.22)',
      }}>
        <p style={{ fontSize:'10px', color:'rgba(255,255,255,0.55)', fontFamily:"'DM Sans',sans-serif", lineHeight:1.5 }}>
          Live prices · Web search · Document analysis
        </p>
        <div style={{ marginTop:'12px', height:'1px', background:'rgba(200,146,42,0.4)' }}/>
        <p style={{ fontSize:'9px', color:'rgba(200,146,42,0.85)', fontFamily:'JetBrains Mono,monospace', marginTop:'10px', letterSpacing:'0.8px' }}>
          GROWTH GRADUAL · IN THE MONEY
        </p>
      </div>

    </div>
  );
}
