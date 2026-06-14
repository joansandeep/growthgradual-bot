/**
 * GET /api/report
 * Generates a market news PDF report from the growth_gradual_cache.json file.
 * Uses jsPDF-style HTML → browser print, or falls back to a raw HTML download.
 *
 * For server-side PDF generation we build a self-contained HTML page and
 * return it with a Content-Disposition: attachment header so the browser
 * treats it as a download. For a true server-side PDF, swap the body for
 * a Puppeteer/playwright call — but this approach is zero-dependency.
 */

import { NextRequest, NextResponse } from 'next/server';
import { promises as fs } from 'fs';
import path from 'path';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const CACHE_PATH = path.join(process.cwd(), 'growth_gradual_cache.json');

interface CachedArticle {
  id: string; title: string; source: string; url: string;
  time: string; time_ms: number; tag: string; category: string; summary: string;
}

const CATEGORY_LABELS: Record<string, string> = {
  all:          '📊 Markets',
  stocks:       '📈 Stocks',
  banks:        '🏦 Banking',
  mutual_funds: '💰 Mutual Funds',
  finance:      '🏛️ Economy & Finance',
};

function escapeHtml(s: string) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function buildHtml(articles: CachedArticle[], fetchedAt: number, sources: string[]): string {
  const date = new Date(fetchedAt).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    dateStyle: 'full',
    timeStyle: 'short',
  });

  const byCategory: Record<string, CachedArticle[]> = {};
  for (const a of articles) {
    const cat = a.category || 'all';
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push(a);
  }

  const categoryOrder = ['all', 'stocks', 'banks', 'mutual_funds', 'finance'];
  const orderedCats = [
    ...categoryOrder.filter(c => byCategory[c]),
    ...Object.keys(byCategory).filter(c => !categoryOrder.includes(c)),
  ];

  let sections = '';
  for (const cat of orderedCats) {
    const arts = byCategory[cat];
    const label = CATEGORY_LABELS[cat] ?? cat.replace(/_/g, ' ').toUpperCase();
    const rows = arts
      .map((a, i) => `
        <tr class="${i % 2 === 0 ? 'even' : 'odd'}">
          <td class="num">${i + 1}</td>
          <td>
            <a href="${escapeHtml(a.url)}" class="article-link">${escapeHtml(a.title)}</a>
            ${a.summary ? `<div class="summary">${escapeHtml(a.summary.slice(0, 160))}</div>` : ''}
          </td>
          <td class="source">${escapeHtml(a.source)}</td>
          <td class="tag">${escapeHtml(a.tag)}</td>
        </tr>`)
      .join('');

    sections += `
      <div class="section">
        <h2 class="section-title">${label}</h2>
        <table>
          <thead>
            <tr>
              <th class="num">#</th>
              <th>Headline</th>
              <th>Source</th>
              <th>Tag</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  const sourceList = sources.map(s => `<span class="src-badge">${escapeHtml(s)}</span>`).join(' ');

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Finbot Market Report — ${date}</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
      color: #1a1a2e;
      background: #fff;
      padding: 32px 40px;
      max-width: 960px;
      margin: auto;
    }
    /* ─── Cover ─── */
    .cover {
      border-bottom: 3px solid #2563eb;
      padding-bottom: 24px;
      margin-bottom: 32px;
    }
    .logo-row { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
    .logo-dot {
      width: 36px; height: 36px; border-radius: 50%;
      background: linear-gradient(135deg, #2563eb, #7c3aed);
      display: flex; align-items: center; justify-content: center;
      color: #fff; font-weight: 900; font-size: 18px;
    }
    .logo-name { font-size: 22px; font-weight: 700; color: #2563eb; }
    h1 { font-size: 28px; font-weight: 800; color: #1a1a2e; margin-bottom: 6px; }
    .meta { font-size: 13px; color: #6b7280; }
    .meta strong { color: #374151; }
    /* ─── Summary strip ─── */
    .summary-strip {
      display: flex; gap: 16px; flex-wrap: wrap;
      background: #f0f4ff; border-radius: 10px;
      padding: 16px 20px; margin-bottom: 32px;
    }
    .stat { text-align: center; }
    .stat .val { font-size: 26px; font-weight: 800; color: #2563eb; }
    .stat .lbl { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: .5px; }
    /* ─── Sources ─── */
    .src-section { margin-bottom: 28px; }
    .src-section h3 { font-size: 13px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 8px; }
    .src-badge {
      display: inline-block; background: #e0e7ff; color: #3730a3;
      border-radius: 20px; padding: 2px 10px; font-size: 11px; font-weight: 600; margin: 2px;
    }
    /* ─── Sections ─── */
    .section { margin-bottom: 36px; break-inside: avoid; }
    .section-title {
      font-size: 16px; font-weight: 700; color: #1e40af;
      border-left: 4px solid #2563eb; padding-left: 10px;
      margin-bottom: 12px;
    }
    table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    thead tr { background: #1e3a8a; color: #fff; }
    th { padding: 8px 10px; text-align: left; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
    td { padding: 7px 10px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
    tr.even td { background: #f8faff; }
    tr.odd td { background: #fff; }
    tr:hover td { background: #eff6ff; }
    .num { width: 36px; color: #9ca3af; text-align: center; font-size: 11px; }
    .article-link { color: #1d4ed8; text-decoration: none; font-weight: 500; }
    .article-link:hover { text-decoration: underline; }
    .summary { font-size: 11px; color: #6b7280; margin-top: 3px; font-style: italic; line-height: 1.4; }
    .source { width: 130px; color: #374151; font-size: 11px; font-weight: 600; }
    .tag { width: 90px; }
    .tag { font-size: 10px; background: #dbeafe; color: #1e40af; border-radius: 4px; padding: 2px 6px; white-space: nowrap; }
    /* ─── Footer ─── */
    .footer {
      margin-top: 40px; padding-top: 16px;
      border-top: 1px solid #e5e7eb;
      font-size: 11px; color: #9ca3af; text-align: center;
    }
    /* ─── Print ─── */
    @media print {
      body { padding: 20px; }
      .section { break-inside: avoid; }
      a { color: #1d4ed8 !important; }
      .no-print { display: none !important; }
    }
    /* ─── Print button (web only) ─── */
    .print-bar {
      position: fixed; top: 16px; right: 20px;
      display: flex; gap: 8px;
    }
    .btn {
      padding: 8px 18px; border: none; border-radius: 6px;
      font-size: 13px; font-weight: 600; cursor: pointer;
    }
    .btn-primary { background: #2563eb; color: #fff; }
    .btn-primary:hover { background: #1d4ed8; }
    .btn-secondary { background: #f3f4f6; color: #374151; }
    .btn-secondary:hover { background: #e5e7eb; }
  </style>
</head>
<body>

  <div class="print-bar no-print">
    <button class="btn btn-primary" onclick="window.print()">🖨️ Save as PDF</button>
    <button class="btn btn-secondary" onclick="window.close()">✕ Close</button>
  </div>

  <div class="cover">
    <div class="logo-row">
      <div class="logo-dot">G</div>
      <span class="logo-name">Growth Gradual</span>
    </div>
    <h1>Market Intelligence Report</h1>
    <p class="meta">
      Generated: <strong>${date} IST</strong> &nbsp;|&nbsp;
      Source: Live scraper cache &nbsp;|&nbsp;
      Confidential — Internal use only
    </p>
  </div>

  <div class="summary-strip">
    <div class="stat"><div class="val">${articles.length}</div><div class="lbl">Total Articles</div></div>
    <div class="stat"><div class="val">${sources.length}</div><div class="lbl">Sources Tracked</div></div>
    <div class="stat"><div class="val">${orderedCats.length}</div><div class="lbl">Categories</div></div>
    <div class="stat"><div class="val">${new Date(fetchedAt).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' })}</div><div class="lbl">Cache Time (IST)</div></div>
  </div>

  <div class="src-section">
    <h3>Active Sources</h3>
    ${sourceList}
  </div>

  ${sections}

  <div class="footer">
    Growth Gradual Finbot &nbsp;·&nbsp; Data sourced from public financial news portals &nbsp;·&nbsp;
    Not financial advice — verify with live sources before trading.
  </div>

</body>
</html>`;
}

export async function GET(_req: NextRequest) {
  let articles: CachedArticle[] = [];
  let fetchedAt = Date.now();
  let sources: string[] = [];

  try {
    const raw = await fs.readFile(CACHE_PATH, 'utf8');
    const data = JSON.parse(raw);
    articles = data.articles ?? [];
    fetchedAt = data.fetched_at ?? data.fetchedAt ?? Date.now();
    sources = data.sources ?? [];
  } catch (e) {
    return NextResponse.json(
      { error: 'Cache not found. Run /api/scrape first to populate the cache.' },
      { status: 503 },
    );
  }

  if (!articles.length) {
    return NextResponse.json(
      { error: 'Cache is empty. Trigger a scrape at /api/scrape first.' },
      { status: 503 },
    );
  }

  const html = buildHtml(articles, fetchedAt, sources);
  const dateStr = new Date(fetchedAt).toISOString().slice(0, 10);

  return new Response(html, {
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      // Open in browser — user clicks "Save as PDF" via print dialog
      'Content-Disposition': `inline; filename="finbot-report-${dateStr}.html"`,
      'Cache-Control': 'no-cache',
    },
  });
}
