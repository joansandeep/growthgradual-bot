'use client';
import { useMemo, useState } from 'react';
import { DataPoint, DataSearchResult } from '@/types';

function parseNumeric(value: string | number): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  const cleaned = value.replace(/,/g, '').trim();
  const m = cleaned.match(/-?\d+(\.\d+)?/);
  if (!m) return null;
  const n = parseFloat(m[0]);
  return Number.isFinite(n) ? n : null;
}

const CHART_COLORS = ['#0d5c45', '#c8922a', '#1a1f4e', '#7f1d1d', '#5b21b6', '#0f6b50'];

function MetricChart({ metric, points }: { metric: string; points: DataPoint[] }) {
  const rows = points
    .map(p => ({ ...p, num: parseNumeric(p.value) }))
    .filter(p => p.num !== null) as (DataPoint & { num: number })[];
  if (rows.length < 2) return null;

  const max = Math.max(...rows.map(r => Math.abs(r.num)), 1);
  const barH = 26;
  const gap = 10;
  const height = rows.length * (barH + gap) + gap;

  return (
    <div style={{
      background: '#fff', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)',
      padding: '16px 18px', boxShadow: 'var(--shadow-sm)',
    }}>
      <p style={{
        fontSize: '10px', fontWeight: 700, color: 'var(--muted)', textTransform: 'uppercase',
        letterSpacing: '1.5px', fontFamily: "'DM Sans',sans-serif", marginBottom: '12px',
      }}>{metric}</p>
      <svg viewBox={`0 0 400 ${height}`} width="100%" height={height}>
        {rows.map((r, i) => {
          const w = Math.max((Math.abs(r.num) / max) * 230, 2);
          const y = gap + i * (barH + gap);
          return (
            <g key={i}>
              <text x={0} y={y + barH / 2 + 4} fontSize="10.5" fontFamily="'DM Sans',sans-serif" fill="#3d4a5c">
                {r.entity.length > 22 ? r.entity.slice(0, 21) + '…' : r.entity}
              </text>
              <rect x={140} y={y} width={w} height={barH} rx={4} fill={CHART_COLORS[i % CHART_COLORS.length]} opacity={0.88} />
              <text x={140 + w + 8} y={y + barH / 2 + 4} fontSize="11" fontWeight={600} fontFamily="'JetBrains Mono',monospace" fill="#1a2035">
                {r.value}{r.unit ? ` ${r.unit}` : ''}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function KpiCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div style={{
      background: '#fff', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)',
      padding: '14px 16px', boxShadow: 'var(--shadow-xs)', flex: '1 1 140px', minWidth: '140px',
    }}>
      <p style={{ fontSize: '20px', fontWeight: 700, color: 'var(--navy2)', fontFamily: "'Playfair Display',serif" }}>{value}</p>
      <p style={{ fontSize: '9.5px', color: 'var(--muted)', fontFamily: "'DM Sans',sans-serif", textTransform: 'uppercase', letterSpacing: '1px', marginTop: '2px' }}>{label}</p>
    </div>
  );
}

export default function DataDashboard({ result }: { result: DataSearchResult }) {
  const [exporting, setExporting] = useState(false);
  const { dataPoints, query } = result;

  const grouped = useMemo(() => {
    const m = new Map<string, DataPoint[]>();
    for (const p of dataPoints) {
      if (!m.has(p.metric)) m.set(p.metric, []);
      m.get(p.metric)!.push(p);
    }
    return Array.from(m.entries());
  }, [dataPoints]);

  const uniqueEntities = useMemo(() => new Set(dataPoints.map(p => p.entity)).size, [dataPoints]);
  const liveCount = useMemo(() => dataPoints.filter(p => p.kind === 'live').length, [dataPoints]);
  const sourceCount = result.sourceCount ?? result.sources?.length ?? 0;

  const filename = () => {
    const slug = query.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '').slice(0, 50) || 'data-search';
    return slug;
  };

  const exportFile = async (format: 'xlsx' | 'csv') => {
    setExporting(true);
    try {
      const XLSX = await import('xlsx');
      const rows = dataPoints.map(p => ({
        Entity: p.entity,
        Metric: p.metric,
        Value: p.value,
        Unit: p.unit,
        Period: p.period,
        Source: p.sourceTitle,
        'Source URL': p.sourceUrl,
        Type: p.kind === 'live' ? 'Live Market Data' : 'Web Search',
      }));
      const ws = XLSX.utils.json_to_sheet(rows);
      ws['!cols'] = [{ wch: 24 }, { wch: 22 }, { wch: 14 }, { wch: 10 }, { wch: 10 }, { wch: 28 }, { wch: 34 }, { wch: 14 }];
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, 'Data Points');
      if (format === 'xlsx') {
        XLSX.writeFile(wb, `${filename()}.xlsx`);
      } else {
        const csv = XLSX.utils.sheet_to_csv(ws);
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${filename()}.csv`;
        a.click();
        URL.revokeObjectURL(url);
      }
    } finally {
      setExporting(false);
    }
  };

  if (dataPoints.length === 0) {
    return (
      <div style={{
        background: '#fff', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)',
        padding: '24px', textAlign: 'center', color: 'var(--muted)', fontFamily: "'DM Sans',sans-serif", fontSize: '13px',
      }}>
        No concrete data points were found for this query. Try naming specific companies, metrics, or a time period.
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'stretch' }}>
        <KpiCard label="Sources Scanned" value={sourceCount} />
        <KpiCard label="Data Points" value={dataPoints.length} />
        <KpiCard label="Entities" value={uniqueEntities} />
        <KpiCard label="Metrics" value={grouped.length} />
        <KpiCard label="Live Market Figures" value={liveCount} />
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <button
            onClick={() => exportFile('xlsx')}
            disabled={exporting}
            style={{
              background: 'var(--teal)', color: '#fff', border: 'none', borderRadius: 'var(--radius-sm)',
              padding: '10px 16px', fontSize: '12px', fontWeight: 600, fontFamily: "'DM Sans',sans-serif",
              cursor: exporting ? 'default' : 'pointer', opacity: exporting ? 0.6 : 1,
            }}
          >⬇ Export Excel</button>
          <button
            onClick={() => exportFile('csv')}
            disabled={exporting}
            style={{
              background: '#fff', color: 'var(--teal)', border: '1px solid var(--teal)', borderRadius: 'var(--radius-sm)',
              padding: '10px 16px', fontSize: '12px', fontWeight: 600, fontFamily: "'DM Sans',sans-serif",
              cursor: exporting ? 'default' : 'pointer', opacity: exporting ? 0.6 : 1,
            }}
          >⬇ Export CSV</button>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '14px' }}>
        {grouped.map(([metric, points]) => <MetricChart key={metric} metric={metric} points={points} />)}
      </div>

      <div style={{
        background: '#fff', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)',
        overflow: 'hidden', boxShadow: 'var(--shadow-sm)',
      }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px', fontFamily: "'DM Sans',sans-serif" }}>
            <thead>
              <tr style={{ background: 'var(--surface2)', borderBottom: '1px solid var(--border)' }}>
                {['Entity', 'Metric', 'Value', 'Unit', 'Period', 'Source'].map(h => (
                  <th key={h} style={{ textAlign: 'left', padding: '10px 14px', fontSize: '9.5px', textTransform: 'uppercase', letterSpacing: '1px', color: 'var(--muted)', fontWeight: 700 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dataPoints.map((p, i) => (
                <tr key={i} style={{ borderBottom: '1px solid var(--border2)' }}>
                  <td style={{ padding: '9px 14px', fontWeight: 600, color: 'var(--text)' }}>{p.entity}</td>
                  <td style={{ padding: '9px 14px', color: 'var(--text2)' }}>{p.metric}</td>
                  <td style={{ padding: '9px 14px', fontFamily: "'JetBrains Mono',monospace", fontWeight: 600 }}>{p.value}</td>
                  <td style={{ padding: '9px 14px', color: 'var(--muted)' }}>{p.unit || '—'}</td>
                  <td style={{ padding: '9px 14px', color: 'var(--muted)' }}>{p.period || '—'}</td>
                  <td style={{ padding: '9px 14px' }}>
                    {p.sourceUrl ? (
                      <a href={p.sourceUrl} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--teal)', textDecoration: 'none' }}>
                        {p.sourceTitle || 'Source'} ↗
                      </a>
                    ) : (
                      <span style={{ color: 'var(--muted)' }}>{p.sourceTitle || '—'}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
