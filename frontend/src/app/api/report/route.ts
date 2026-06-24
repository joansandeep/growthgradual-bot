/**
 * GET /api/report
 * Generates a visually rich 10-12 page market intelligence HTML report.
 *
 * Pages / sections:
 *   1.  Cover page — logo, date, headline stats
 *   2.  Executive Summary — top story per category with hero image
 *   3.  Market Snapshot — overview bar chart (Datawrapper) + article count
 *   4.  Top Stories Grid — featured article cards with thumbnails
 *   5+. Category Deep-Dives — one section per category with DW table + article cards + images
 *   N-1 Source Analysis — DW table of source distribution
 *   N.  Appendix / Disclaimer
 *
 * Charts: Datawrapper (DATAWRAPPER_API_TOKEN env var). Falls back to plain HTML.
 * Images: routed through /api/image-proxy to bypass hotlink protection.
 *
 * Requires: DATAWRAPPER_API_TOKEN (optional — degrades gracefully without it)
 */

import { NextRequest, NextResponse } from 'next/server';
import { promises as fs } from 'fs';
import path from 'path';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const CACHE_PATH = path.join(process.cwd(), 'growth_gradual_cache.json');
const DW_API = 'https://api.datawrapper.de/v3';
const DW_TOKEN = process.env.DATAWRAPPER_API_TOKEN ?? '';

// ─── Types ────────────────────────────────────────────────────────────────────

interface CachedArticle {
  id: string;
  title: string;
  source: string;
  url: string;
  time: string;
  time_ms: number;
  tag: string;
  category: string;
  summary: string;
  image?: string;
}

const CATEGORY_LABELS: Record<string, string> = {
  all:          '📊 Markets Overview',
  stocks:       '📈 Stocks & Equities',
  banks:        '🏦 Banking & Finance',
  mutual_funds: '💰 Mutual Funds',
  finance:      '🏛️ Economy & Policy',
};
const CATEGORY_DW: Record<string, string> = {
  all:          'Markets',
  stocks:       'Stocks',
  banks:        'Banking',
  mutual_funds: 'Mutual Funds',
  finance:      'Economy',
};
// Accent colour per category (for section headers / decorative bars)
const CATEGORY_COLOR: Record<string, string> = {
  all:          '#2563eb',
  stocks:       '#16a34a',
  banks:        '#dc2626',
  mutual_funds: '#7c3aed',
  finance:      '#d97706',
};
const CATEGORY_BG: Record<string, string> = {
  all:          '#eff6ff',
  stocks:       '#f0fdf4',
  banks:        '#fef2f2',
  mutual_funds: '#f5f3ff',
  finance:      '#fffbeb',
};

// ─── Helpers ──────────────────────────────────────────────────────────────────

function esc(s: string) {
  return s
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** Proxy image URL so hotlink-protected domains load in the report */
function proxyImg(url: string | undefined): string | null {
  if (!url) return null;
  return `/api/image-proxy?url=${encodeURIComponent(url)}`;
}

/** Safe image tag with fallback; hides broken images */
function imgTag(url: string | undefined, alt: string, cls = ''): string {
  const src = proxyImg(url);
  if (!src) return '';
  return `<img src="${esc(src)}" alt="${esc(alt)}" class="${cls}" loading="lazy" onerror="this.style.display='none'"/>`;
}

function csvField(s: string): string {
  const c = s.replace(/"/g, '""').replace(/\n/g, ' ');
  return c.includes(',') || c.includes('"') ? `"${c}"` : c;
}

// ─── Datawrapper ──────────────────────────────────────────────────────────────

const DW_HEADERS = {
  Authorization: `Bearer ${DW_TOKEN}`,
  'Content-Type': 'application/json',
};

async function createAndPublishChart(opts: {
  type: string;
  title: string;
  csvData: string;
  metadata?: Record<string, unknown>;
}): Promise<string | null> {
  if (!DW_TOKEN) return null;
  try {
    const cr = await fetch(`${DW_API}/charts`, {
      method: 'POST', headers: DW_HEADERS,
      body: JSON.stringify({ title: opts.title, type: opts.type }),
    });
    if (!cr.ok) return null;
    const { id } = await cr.json();

    const dr = await fetch(`${DW_API}/charts/${id}/data`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${DW_TOKEN}`, 'Content-Type': 'text/csv' },
      body: opts.csvData,
    });
    if (!dr.ok) return null;

    if (opts.metadata) {
      await fetch(`${DW_API}/charts/${id}`, {
        method: 'PATCH', headers: DW_HEADERS,
        body: JSON.stringify({ metadata: opts.metadata }),
      });
    }

    const pr = await fetch(`${DW_API}/charts/${id}/publish`, {
      method: 'POST', headers: DW_HEADERS,
    });
    if (!pr.ok) return null;

    return `https://datawrapper.dwcdn.net/${id}/1/`;
  } catch { return null; }
}

function dwIframe(url: string, title: string, height = 400): string {
  return `
  <div class="dw-wrap">
    <iframe
      title="${esc(title)}"
      aria-label="${esc(title)}"
      src="${esc(url)}"
      scrolling="no" frameborder="0"
      style="width:100%;border:none;"
      height="${height}"
      data-external="1"
    ></iframe>
    <script>
      !function(){"use strict";window.addEventListener("message",(function(a){if(void 0!==a.data["datawrapper-height"]){var e=document.querySelectorAll("iframe");for(var t in a.data["datawrapper-height"])for(var r=0;r<e.length;r++)if(e[r].contentWindow===a.source)e[r].style.height=a.data["datawrapper-height"][t]+"px"}}));}();
    </script>
  </div>`;
}

// ─── CSV builders ─────────────────────────────────────────────────────────────

function buildCatCountCsv(byCategory: Record<string, CachedArticle[]>, cats: string[]): string {
  return ['Category,Articles',
    ...cats.map(c => `${CATEGORY_DW[c] ?? c},${byCategory[c].length}`)
  ].join('\n');
}

function buildArticleTableCsv(articles: CachedArticle[]): string {
  return ['#,Headline,Source,Tag,Time',
    ...articles.slice(0, 20).map((a, i) =>
      [i + 1, csvField(a.title), csvField(a.source), csvField(a.tag || '—'), csvField(a.time || '—')].join(','))
  ].join('\n');
}

function buildSourceCsv(articles: CachedArticle[]): string {
  const counts: Record<string, number> = {};
  for (const a of articles) counts[a.source] = (counts[a.source] || 0) + 1;
  const sorted = Object.entries(counts).sort((x, y) => y[1] - x[1]).slice(0, 15);
  return ['Source,Articles', ...sorted.map(([s, n]) => `${csvField(s)},${n}`)].join('\n');
}

// ─── HTML fragments ───────────────────────────────────────────────────────────

/** A featured article card with image + summary */
function articleCard(a: CachedArticle, size: 'large' | 'small' = 'small'): string {
  const img = imgTag(a.image, a.title, size === 'large' ? 'card-img-large' : 'card-img');
  const colorKey = a.category || 'all';
  const accent = CATEGORY_COLOR[colorKey] ?? '#2563eb';
  return `
  <div class="article-card card-${size}">
    ${img ? `<div class="card-img-wrap">${img}</div>` : ''}
    <div class="card-body">
      <span class="tag-pill" style="background:${accent}20;color:${accent}">${esc(a.tag || a.category)}</span>
      <div class="card-title"><a href="${esc(a.url)}" class="card-link">${esc(a.title)}</a></div>
      ${a.summary ? `<div class="card-summary">${esc(a.summary.slice(0, size === 'large' ? 240 : 160))}</div>` : ''}
      <div class="card-meta">${esc(a.source)} · ${esc(a.time)}</div>
    </div>
  </div>`;
}

/** Fallback plain table when DW is unavailable */
function fallbackTable(articles: CachedArticle[]): string {
  const rows = articles.slice(0, 15).map((a, i) => `
    <tr class="${i % 2 === 0 ? 'even' : 'odd'}">
      <td class="num">${i + 1}</td>
      <td>
        <a href="${esc(a.url)}" class="fb-link">${esc(a.title)}</a>
        ${a.summary ? `<div class="fb-summary">${esc(a.summary.slice(0, 160))}</div>` : ''}
      </td>
      <td class="fb-src">${esc(a.source)}</td>
      <td><span class="fb-tag">${esc(a.tag || '—')}</span></td>
    </tr>`).join('');
  return `<table class="fb-table"><thead><tr>
    <th class="num">#</th><th>Headline</th><th>Source</th><th>Tag</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

// ─── Full report builder ──────────────────────────────────────────────────────

async function buildHtml(
  articles: CachedArticle[],
  fetchedAt: number,
  sources: string[],
): Promise<string> {
  const date = new Date(fetchedAt).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata', dateStyle: 'full', timeStyle: 'short',
  });
  const shortDate = new Date(fetchedAt).toLocaleDateString('en-IN', {
    timeZone: 'Asia/Kolkata', day: 'numeric', month: 'long', year: 'numeric',
  });

  // Group articles by category
  const byCategory: Record<string, CachedArticle[]> = {};
  for (const a of articles) {
    const cat = a.category || 'all';
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push(a);
  }
  const catOrder = ['all', 'stocks', 'banks', 'mutual_funds', 'finance'];
  const orderedCats = [
    ...catOrder.filter(c => byCategory[c]),
    ...Object.keys(byCategory).filter(c => !catOrder.includes(c)),
  ];

  // ── Fire all Datawrapper calls in parallel ────────────────────────────────

  const [
    overviewDwUrl,
    sourceDwUrl,
    ...catDwUrls
  ] = await Promise.all([
    // 1. Overview bar chart
    createAndPublishChart({
      type: 'd3-bars',
      title: 'Market Coverage by Category',
      csvData: buildCatCountCsv(byCategory, orderedCats),
      metadata: {
        visualize: {
          'base-color': '#2563eb', thick: true,
          'label-colors': true, 'value-labels': 'outside',
        },
        describe: {
          intro: `Article distribution across categories — ${shortDate}`,
          byline: 'Growth Gradual Finbot',
          'source-name': 'Growth Gradual Cache',
        },
      },
    }),
    // 2. Source distribution table
    createAndPublishChart({
      type: 'tables',
      title: 'Source Distribution',
      csvData: buildSourceCsv(articles),
      metadata: {
        visualize: { 'striped-rows': true, 'column-widths': { '0': 220, '1': 80 } },
        describe: { byline: 'Growth Gradual Finbot', 'source-name': 'Growth Gradual Cache' },
      },
    }),
    // 3. Per-category article tables
    ...orderedCats.map(cat =>
      createAndPublishChart({
        type: 'tables',
        title: `${CATEGORY_DW[cat] ?? cat} — Latest Articles`,
        csvData: buildArticleTableCsv(byCategory[cat]),
        metadata: {
          visualize: {
            'striped-rows': true,
            'frozen-columns': 0,
            'column-widths': { '0': 40, '2': 110, '3': 80, '4': 80 },
          },
          describe: { byline: 'Growth Gradual Finbot', 'source-name': 'Growth Gradual Cache' },
        },
      }),
    ),
  ]);

  // ── Source distribution stats ─────────────────────────────────────────────
  const srcCounts: Record<string, number> = {};
  for (const a of articles) srcCounts[a.source] = (srcCounts[a.source] || 0) + 1;
  const topSources = Object.entries(srcCounts).sort((x, y) => y[1] - x[1]).slice(0, 5);

  // ── Articles with images (for featured grids) ─────────────────────────────
  const withImage = articles.filter(a => a.image);
  const topFeatured = withImage.slice(0, 6);

  // ── Per-category top story (for executive summary) ────────────────────────
  const topStories = orderedCats.map(cat => byCategory[cat][0]).filter(Boolean);

  // ── Tag distribution ──────────────────────────────────────────────────────
  const tagCounts: Record<string, number> = {};
  for (const a of articles) if (a.tag) tagCounts[a.tag] = (tagCounts[a.tag] || 0) + 1;
  const topTags = Object.entries(tagCounts).sort((x, y) => y[1] - x[1]).slice(0, 10);

  // ═══════════════════════════════════════════════════════════════════════════
  // PAGE 1 — COVER
  // ═══════════════════════════════════════════════════════════════════════════
  const coverPage = `
  <div class="page page-cover">
    <div class="cover-bg"></div>
    <div class="cover-content">
      <div class="cover-logo-row">
        <div class="cover-logo-dot">G</div>
        <span class="cover-logo-name">Growth Gradual</span>
      </div>
      <div class="cover-eyebrow">FINBOT PREMIUM · MARKET INTELLIGENCE</div>
      <h1 class="cover-title">Daily Market<br/>Intelligence Report</h1>
      <div class="cover-date">${shortDate}</div>
      <div class="cover-stats-row">
        <div class="cover-stat">
          <div class="cs-val">${articles.length}</div>
          <div class="cs-lbl">Articles Tracked</div>
        </div>
        <div class="cover-stat-div"></div>
        <div class="cover-stat">
          <div class="cs-val">${sources.length}</div>
          <div class="cs-lbl">Live Sources</div>
        </div>
        <div class="cover-stat-div"></div>
        <div class="cover-stat">
          <div class="cs-val">${orderedCats.length}</div>
          <div class="cs-lbl">Categories</div>
        </div>
        <div class="cover-stat-div"></div>
        <div class="cover-stat">
          <div class="cs-val">${withImage.length}</div>
          <div class="cs-lbl">Stories with Images</div>
        </div>
      </div>
      <div class="cover-footer-note">
        Generated ${date} IST · Confidential — Internal use only · Not financial advice
      </div>
    </div>
  </div>`;

  // ═══════════════════════════════════════════════════════════════════════════
  // PAGE 2 — EXECUTIVE SUMMARY
  // ═══════════════════════════════════════════════════════════════════════════
  const execSummaryCards = topStories.slice(0, 5).map(a => {
    const color = CATEGORY_COLOR[a.category] ?? '#2563eb';
    const bg = CATEGORY_BG[a.category] ?? '#eff6ff';
    return `
    <div class="exec-card" style="border-left-color:${color};background:${bg}">
      <div class="exec-cat" style="color:${color}">${CATEGORY_LABELS[a.category] ?? a.category}</div>
      <div class="exec-title"><a href="${esc(a.url)}" class="exec-link">${esc(a.title)}</a></div>
      ${a.summary ? `<div class="exec-summary">${esc(a.summary.slice(0, 200))}</div>` : ''}
      <div class="exec-meta">${esc(a.source)} · ${esc(a.time)}</div>
    </div>`;
  }).join('');

  const execPage = `
  <div class="page">
    <div class="page-header" style="border-color:#2563eb">
      <div class="ph-badge" style="background:#2563eb">02</div>
      <h2 class="ph-title">Executive Summary</h2>
      <div class="ph-sub">Top story from each market category</div>
    </div>
    <div class="exec-grid">${execSummaryCards}</div>
  </div>`;

  // ═══════════════════════════════════════════════════════════════════════════
  // PAGE 3 — MARKET SNAPSHOT (overview chart + tag cloud)
  // ═══════════════════════════════════════════════════════════════════════════
  const tagCloud = topTags.map(([tag, n]) => {
    const size = Math.max(11, Math.min(22, 11 + n));
    return `<span class="tag-cloud-item" style="font-size:${size}px">${esc(tag)} <span class="tc-count">${n}</span></span>`;
  }).join(' ');

  const overviewChartHtml = overviewDwUrl
    ? dwIframe(overviewDwUrl, 'Market Coverage by Category', 340)
    : `<div class="dw-placeholder">
        ${orderedCats.map(cat => `
          <div class="dw-bar-row">
            <div class="dw-bar-label">${CATEGORY_DW[cat] ?? cat}</div>
            <div class="dw-bar-track">
              <div class="dw-bar-fill" style="width:${Math.min(100, (byCategory[cat].length / articles.length) * 100 * 3)}%;background:${CATEGORY_COLOR[cat] ?? '#2563eb'}"></div>
            </div>
            <div class="dw-bar-val">${byCategory[cat].length}</div>
          </div>`).join('')}
       </div>`;

  const snapshotPage = `
  <div class="page">
    <div class="page-header" style="border-color:#7c3aed">
      <div class="ph-badge" style="background:#7c3aed">03</div>
      <h2 class="ph-title">Market Snapshot</h2>
      <div class="ph-sub">Coverage distribution &amp; trending themes</div>
    </div>
    <div class="two-col">
      <div class="col-main">
        <div class="section-subtitle">📊 Coverage by Category</div>
        ${overviewChartHtml}
      </div>
      <div class="col-side">
        <div class="section-subtitle">🏷️ Trending Topics</div>
        <div class="tag-cloud">${tagCloud}</div>
        <div class="section-subtitle" style="margin-top:20px">📰 Top Sources</div>
        <div class="src-rank">
          ${topSources.map(([s, n], i) => `
            <div class="src-rank-row">
              <div class="src-rank-num">${i + 1}</div>
              <div class="src-rank-name">${esc(s)}</div>
              <div class="src-rank-bar-wrap">
                <div class="src-rank-bar" style="width:${Math.round((n / topSources[0][1]) * 100)}%"></div>
              </div>
              <div class="src-rank-val">${n}</div>
            </div>`).join('')}
        </div>
      </div>
    </div>
  </div>`;

  // ═══════════════════════════════════════════════════════════════════════════
  // PAGE 4 — FEATURED STORIES (image grid)
  // ═══════════════════════════════════════════════════════════════════════════
  const featuredGrid = topFeatured.length > 0
    ? `<div class="featured-grid">
        ${topFeatured.map((a, i) => articleCard(a, i === 0 ? 'large' : 'small')).join('')}
       </div>`
    : `<div class="no-images-note">No thumbnail images were available in today's RSS feeds.</div>
       <div class="featured-grid">
         ${articles.slice(0, 6).map(a => articleCard(a, 'small')).join('')}
       </div>`;

  const featuredPage = `
  <div class="page">
    <div class="page-header" style="border-color:#16a34a">
      <div class="ph-badge" style="background:#16a34a">04</div>
      <h2 class="ph-title">Featured Stories</h2>
      <div class="ph-sub">Highlighted articles from today's financial news</div>
    </div>
    ${featuredGrid}
  </div>`;

  // ═══════════════════════════════════════════════════════════════════════════
  // PAGES 5+ — CATEGORY DEEP-DIVES
  // ═══════════════════════════════════════════════════════════════════════════
  const categoryPages = orderedCats.map((cat, idx) => {
    const arts = byCategory[cat];
    const label = CATEGORY_LABELS[cat] ?? cat.replace(/_/g, ' ').toUpperCase();
    const color = CATEGORY_COLOR[cat] ?? '#2563eb';
    const bg = CATEGORY_BG[cat] ?? '#eff6ff';
    const pageNum = String(5 + idx).padStart(2, '0');
    const dwUrl = catDwUrls[idx];

    // Pick 2 articles with images for the mini gallery
    const withImg = arts.filter(a => a.image).slice(0, 2);
    const miniGallery = withImg.length > 0
      ? `<div class="mini-gallery">
          ${withImg.map(a => `
            <div class="mini-card" style="border-top:3px solid ${color}">
              <div class="mini-img-wrap">${imgTag(a.image, a.title, 'mini-img')}</div>
              <div class="mini-card-body">
                <div class="mini-title"><a href="${esc(a.url)}">${esc(a.title)}</a></div>
                <div class="mini-meta">${esc(a.source)} · ${esc(a.time)}</div>
              </div>
            </div>`).join('')}
         </div>`
      : '';

    const chartBlock = dwUrl
      ? dwIframe(dwUrl, `${CATEGORY_DW[cat] ?? cat} Articles`, 480)
      : fallbackTable(arts);

    return `
    <div class="page">
      <div class="page-header" style="border-color:${color};background:${bg}">
        <div class="ph-badge" style="background:${color}">${pageNum}</div>
        <h2 class="ph-title" style="color:${color}">${label}</h2>
        <div class="ph-sub">${arts.length} articles tracked today</div>
      </div>
      ${miniGallery}
      <div class="section-subtitle">📋 All Articles</div>
      ${chartBlock}
    </div>`;
  }).join('');

  // ═══════════════════════════════════════════════════════════════════════════
  // PAGE N-1 — SOURCE ANALYSIS
  // ═══════════════════════════════════════════════════════════════════════════
  const sourcePageNum = String(5 + orderedCats.length).padStart(2, '0');
  const srcChartHtml = sourceDwUrl
    ? dwIframe(sourceDwUrl, 'Source Distribution', 420)
    : fallbackTable(
        Object.entries(srcCounts)
          .sort((x, y) => y[1] - x[1])
          .slice(0, 15)
          .map(([source, n]) => ({ id: source, title: `${source} — ${n} articles`, source, url: '#', time: '', time_ms: 0, tag: String(n), category: '', summary: '' } as CachedArticle))
      );

  const sourcePage = `
  <div class="page">
    <div class="page-header" style="border-color:#0891b2">
      <div class="ph-badge" style="background:#0891b2">${sourcePageNum}</div>
      <h2 class="ph-title">Source Analysis</h2>
      <div class="ph-sub">Coverage distribution across ${sources.length} tracked publishers</div>
    </div>
    <div class="two-col">
      <div class="col-main">
        <div class="section-subtitle">📊 Articles per Publisher</div>
        ${srcChartHtml}
      </div>
      <div class="col-side">
        <div class="section-subtitle">🏅 Publisher Leaderboard</div>
        ${Object.entries(srcCounts).sort((x, y) => y[1] - x[1]).slice(0, 12).map(([s, n], i) => `
          <div class="lb-row ${i < 3 ? 'lb-top' : ''}">
            <div class="lb-rank">${i + 1}</div>
            <div class="lb-name">${esc(s)}</div>
            <div class="lb-count">${n}</div>
          </div>`).join('')}
      </div>
    </div>
  </div>`;

  // ═══════════════════════════════════════════════════════════════════════════
  // PAGE N — APPENDIX / DISCLAIMER
  // ═══════════════════════════════════════════════════════════════════════════
  const appxPageNum = String(6 + orderedCats.length).padStart(2, '0');
  const srcBadges = sources.map(s => `<span class="src-badge">${esc(s)}</span>`).join(' ');

  const appendixPage = `
  <div class="page page-appendix">
    <div class="page-header" style="border-color:#6b7280">
      <div class="ph-badge" style="background:#6b7280">${appxPageNum}</div>
      <h2 class="ph-title">Appendix &amp; Disclaimer</h2>
    </div>
    <div class="appendix-body">
      <h3>Data Sources</h3>
      <p>This report aggregates articles from the following publishers via RSS feeds and public APIs:</p>
      <div class="src-badges-wrap">${srcBadges}</div>

      <h3>Methodology</h3>
      <p>Articles are collected via the Growth Gradual Finbot scraper, which polls RSS feeds across
      Indian and international financial news publishers. Articles are filtered for financial relevance
      using keyword matching and deduplicated by URL hash. Thumbnails are sourced from RSS
      <code>media:thumbnail</code> and <code>media:content</code> tags.</p>

      <h3>Chart Attribution</h3>
      <p>Interactive data visualisations in this report are created and hosted via
      <a href="https://www.datawrapper.de">Datawrapper</a>. Source data: Growth Gradual cache.</p>

      <h3>Disclaimer</h3>
      <p class="disclaimer-text">
        This report is generated automatically for internal informational purposes only. It does
        not constitute financial advice, investment recommendations, or solicitations of any kind.
        All data is sourced from public financial news portals and is provided "as-is" without
        warranty. Market data and news summaries may be delayed, incomplete, or subject to errors.
        Always verify information with primary sources before making any financial decisions.
        Past performance of any securities mentioned is not indicative of future results.
      </p>

      <div class="appendix-footer">
        <div class="af-logo">G</div>
        <div>
          <strong>Growth Gradual Finbot Premium</strong><br/>
          Report generated: ${date} IST<br/>
          Total articles processed: ${articles.length} · Sources: ${sources.length} · Categories: ${orderedCats.length}
        </div>
      </div>
    </div>
  </div>`;

  // ═══════════════════════════════════════════════════════════════════════════
  // FULL HTML
  // ═══════════════════════════════════════════════════════════════════════════
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Growth Gradual Market Report — ${shortDate}</title>
  <style>
    /* ── Reset ── */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
      color: #1a1a2e; background: #f1f5f9;
      padding: 0; margin: 0;
    }
    a { color: inherit; text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* ── Pages ── */
    .page {
      background: #fff;
      width: 100%; max-width: 960px;
      margin: 0 auto 32px;
      border-radius: 12px;
      box-shadow: 0 4px 24px rgba(0,0,0,.08);
      overflow: hidden;
      padding-bottom: 32px;
    }
    @media print {
      body { background: #fff; }
      .page {
        max-width: 100%; margin: 0; border-radius: 0;
        box-shadow: none; page-break-after: always;
        padding-bottom: 20px;
      }
      .no-print { display: none !important; }
      img { max-width: 100%; }
    }

    /* ── Print bar ── */
    .print-bar {
      position: fixed; top: 0; left: 0; right: 0; z-index: 999;
      background: #1e3a8a; padding: 10px 24px;
      display: flex; align-items: center; justify-content: space-between;
    }
    .pb-brand { color: #fff; font-weight: 700; font-size: 15px; }
    .pb-right { display: flex; gap: 10px; }
    .btn { padding: 7px 20px; border: none; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }
    .btn-primary { background: #2563eb; color: #fff; }
    .btn-primary:hover { background: #1d4ed8; }
    .btn-secondary { background: rgba(255,255,255,.15); color: #fff; border: 1px solid rgba(255,255,255,.3); }
    .btn-secondary:hover { background: rgba(255,255,255,.25); }
    .print-spacer { height: 52px; }

    /* ── Cover page ── */
    .page-cover { position: relative; min-height: 560px; background: #0f172a; color: #fff; display: flex; flex-direction: column; }
    .cover-bg {
      position: absolute; inset: 0;
      background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 50%, #312e81 100%);
      opacity: .95;
    }
    /* Decorative grid overlay */
    .cover-bg::after {
      content: '';
      position: absolute; inset: 0;
      background-image:
        linear-gradient(rgba(255,255,255,.04) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.04) 1px, transparent 1px);
      background-size: 40px 40px;
    }
    .cover-content {
      position: relative; z-index: 1;
      padding: 52px 52px 40px;
      display: flex; flex-direction: column; flex: 1;
    }
    .cover-logo-row { display: flex; align-items: center; gap: 12px; margin-bottom: 40px; }
    .cover-logo-dot {
      width: 44px; height: 44px; border-radius: 12px;
      background: linear-gradient(135deg, #3b82f6, #8b5cf6);
      display: flex; align-items: center; justify-content: center;
      font-weight: 900; font-size: 22px; color: #fff;
    }
    .cover-logo-name { font-size: 20px; font-weight: 700; color: #e0e7ff; }
    .cover-eyebrow {
      font-size: 11px; letter-spacing: 3px; font-weight: 600;
      color: #93c5fd; text-transform: uppercase; margin-bottom: 16px;
    }
    .cover-title {
      font-size: 52px; font-weight: 900; line-height: 1.1;
      color: #fff; margin-bottom: 20px;
    }
    .cover-date {
      font-size: 18px; color: #bfdbfe; margin-bottom: 40px;
    }
    .cover-stats-row {
      display: flex; align-items: center; gap: 0;
      background: rgba(255,255,255,.08); border-radius: 12px;
      padding: 20px 24px; margin-bottom: 24px;
      border: 1px solid rgba(255,255,255,.12);
    }
    .cover-stat { flex: 1; text-align: center; }
    .cover-stat-div { width: 1px; background: rgba(255,255,255,.2); height: 40px; }
    .cs-val { font-size: 32px; font-weight: 900; color: #60a5fa; }
    .cs-lbl { font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: .5px; margin-top: 2px; }
    .cover-footer-note { font-size: 11px; color: #64748b; margin-top: auto; }

    /* ── Page header ── */
    .page-header {
      padding: 20px 32px 16px;
      border-left: 6px solid #2563eb;
      border-bottom: 1px solid #e5e7eb;
      display: flex; align-items: center; gap: 16px;
      margin-bottom: 24px;
    }
    .ph-badge {
      width: 32px; height: 32px; border-radius: 8px;
      background: #2563eb; color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-size: 12px; font-weight: 800; flex-shrink: 0;
    }
    .ph-title { font-size: 20px; font-weight: 800; color: #1e3a8a; flex: 1; }
    .ph-sub { font-size: 12px; color: #6b7280; white-space: nowrap; }

    /* ── Section subtitle ── */
    .section-subtitle {
      font-size: 12px; font-weight: 700; color: #6b7280;
      text-transform: uppercase; letter-spacing: 1px;
      margin: 16px 32px 10px;
    }

    /* ── Executive summary ── */
    .exec-grid { padding: 0 32px; display: flex; flex-direction: column; gap: 14px; }
    .exec-card {
      border-left: 4px solid #2563eb;
      border-radius: 0 8px 8px 0;
      padding: 14px 16px;
    }
    .exec-cat { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .8px; margin-bottom: 4px; }
    .exec-title { font-size: 14px; font-weight: 700; line-height: 1.4; margin-bottom: 5px; }
    .exec-link { color: #1d4ed8; }
    .exec-summary { font-size: 12px; color: #4b5563; line-height: 1.5; margin-bottom: 5px; }
    .exec-meta { font-size: 11px; color: #9ca3af; }

    /* ── Two-column layout ── */
    .two-col { display: flex; gap: 0; padding: 0 32px; }
    .col-main { flex: 1.6; padding-right: 24px; border-right: 1px solid #e5e7eb; }
    .col-side { flex: 1; padding-left: 24px; }

    /* ── Tag cloud ── */
    .tag-cloud { line-height: 2; padding: 8px 0; }
    .tag-cloud-item {
      display: inline-block; background: #eff6ff; color: #1d4ed8;
      border-radius: 20px; padding: 2px 10px; margin: 2px 3px;
      font-weight: 600; cursor: default;
    }
    .tc-count { font-size: .75em; color: #60a5fa; }

    /* ── Source rank ── */
    .src-rank { display: flex; flex-direction: column; gap: 8px; }
    .src-rank-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
    .src-rank-num { width: 20px; font-weight: 700; color: #6b7280; flex-shrink: 0; }
    .src-rank-name { flex: 1; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .src-rank-bar-wrap { width: 60px; height: 6px; background: #e5e7eb; border-radius: 3px; flex-shrink: 0; }
    .src-rank-bar { height: 6px; background: #2563eb; border-radius: 3px; }
    .src-rank-val { width: 24px; text-align: right; color: #6b7280; font-size: 11px; }

    /* ── Inline bar chart fallback ── */
    .dw-placeholder { padding: 8px 0; }
    .dw-bar-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 12px; }
    .dw-bar-label { width: 110px; text-align: right; color: #374151; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .dw-bar-track { flex: 1; height: 18px; background: #f1f5f9; border-radius: 4px; overflow: hidden; }
    .dw-bar-fill { height: 18px; border-radius: 4px; min-width: 4px; transition: width .3s; }
    .dw-bar-val { width: 28px; font-weight: 700; color: #1e3a8a; }

    /* ── DW embed ── */
    .dw-wrap { border-radius: 8px; overflow: hidden; border: 1px solid #e5e7eb; }

    /* ── Featured article cards ── */
    .featured-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px; padding: 0 32px;
    }
    .article-card {
      border: 1px solid #e5e7eb; border-radius: 10px;
      overflow: hidden; display: flex; flex-direction: column;
      transition: box-shadow .2s;
    }
    .card-large { grid-column: span 3; flex-direction: row; }
    .card-img-wrap { overflow: hidden; flex-shrink: 0; }
    .card-img { width: 100%; height: 140px; object-fit: cover; display: block; }
    .card-large .card-img-wrap { width: 340px; }
    .card-large .card-img { width: 340px; height: 100%; min-height: 180px; }
    .card-body { padding: 12px; display: flex; flex-direction: column; gap: 5px; }
    .tag-pill {
      display: inline-block; border-radius: 20px; padding: 2px 8px;
      font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .5px;
      align-self: flex-start;
    }
    .card-title { font-size: 13px; font-weight: 700; line-height: 1.4; }
    .card-link { color: #1d4ed8; }
    .card-summary { font-size: 11px; color: #6b7280; line-height: 1.5; }
    .card-meta { font-size: 10px; color: #9ca3af; margin-top: auto; }
    .no-images-note {
      padding: 10px 32px; font-size: 12px; color: #9ca3af; font-style: italic;
      margin-bottom: 8px;
    }

    /* ── Mini gallery (category pages) ── */
    .mini-gallery { display: flex; gap: 16px; padding: 0 32px; margin-bottom: 8px; }
    .mini-card {
      flex: 1; border: 1px solid #e5e7eb; border-radius: 8px;
      overflow: hidden; display: flex; flex-direction: column;
    }
    .mini-img-wrap { overflow: hidden; }
    .mini-img { width: 100%; height: 110px; object-fit: cover; display: block; }
    .mini-card-body { padding: 10px; }
    .mini-title { font-size: 12px; font-weight: 600; line-height: 1.4; margin-bottom: 4px; }
    .mini-title a { color: #1d4ed8; }
    .mini-meta { font-size: 10px; color: #9ca3af; }

    /* ── Fallback table ── */
    .fb-table { width: 100%; border-collapse: collapse; font-size: 12px; margin: 0 32px; width: calc(100% - 64px); }
    .fb-table thead tr { background: #1e3a8a; color: #fff; }
    .fb-table th { padding: 8px 10px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
    .fb-table td { padding: 7px 10px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
    .fb-table tr.even td { background: #f8faff; }
    .fb-table .num { width: 32px; color: #9ca3af; text-align: center; }
    .fb-link { color: #1d4ed8; font-weight: 500; }
    .fb-summary { font-size: 10px; color: #6b7280; margin-top: 2px; font-style: italic; }
    .fb-src { width: 120px; font-size: 11px; font-weight: 600; color: #374151; }
    .fb-tag { font-size: 10px; background: #dbeafe; color: #1e40af; border-radius: 4px; padding: 2px 6px; }

    /* ── Source page leaderboard ── */
    .lb-row {
      display: flex; align-items: center; gap: 10px;
      padding: 6px 0; border-bottom: 1px solid #f1f5f9;
      font-size: 12px;
    }
    .lb-top { font-weight: 700; }
    .lb-rank { width: 24px; height: 24px; border-radius: 50%; background: #e5e7eb; color: #374151; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; flex-shrink: 0; }
    .lb-top .lb-rank { background: #fef08a; color: #92400e; }
    .lb-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .lb-count { background: #dbeafe; color: #1e40af; border-radius: 20px; padding: 2px 8px; font-size: 11px; font-weight: 700; }

    /* ── Appendix ── */
    .page-appendix { background: #fafafa; }
    .appendix-body { padding: 0 40px 32px; }
    .appendix-body h3 { font-size: 14px; font-weight: 700; color: #1e3a8a; margin: 20px 0 6px; }
    .appendix-body p { font-size: 12px; color: #4b5563; line-height: 1.7; }
    .appendix-body code { background: #e5e7eb; border-radius: 3px; padding: 1px 5px; font-size: 11px; }
    .src-badges-wrap { margin: 10px 0; }
    .src-badge {
      display: inline-block; background: #e0e7ff; color: #3730a3;
      border-radius: 20px; padding: 2px 10px; font-size: 11px; font-weight: 600; margin: 2px;
    }
    .disclaimer-text {
      background: #fff7ed; border-left: 3px solid #f59e0b;
      padding: 12px 16px; border-radius: 0 6px 6px 0;
      font-size: 11px !important; color: #78350f !important;
    }
    .appendix-footer {
      display: flex; align-items: center; gap: 16px;
      margin-top: 28px; padding-top: 20px; border-top: 1px solid #e5e7eb;
    }
    .af-logo {
      width: 44px; height: 44px; border-radius: 10px; flex-shrink: 0;
      background: linear-gradient(135deg, #2563eb, #7c3aed);
      display: flex; align-items: center; justify-content: center;
      color: #fff; font-weight: 900; font-size: 20px;
    }
    .appendix-footer div { font-size: 12px; color: #6b7280; line-height: 1.7; }
    .appendix-footer strong { color: #1e3a8a; }
  </style>
</head>
<body>

  <div class="print-bar no-print">
    <span class="pb-brand">📊 Growth Gradual · Market Intelligence</span>
    <div class="pb-right">
      <button class="btn btn-primary" onclick="window.print()">🖨️ Save as PDF</button>
      <button class="btn btn-secondary" onclick="window.close()">✕ Close</button>
    </div>
  </div>
  <div class="print-spacer no-print"></div>

  ${coverPage}
  ${execPage}
  ${snapshotPage}
  ${featuredPage}
  ${categoryPages}
  ${sourcePage}
  ${appendixPage}

</body>
</html>`;
}

// ─── Route handler ────────────────────────────────────────────────────────────

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
  } catch {
    return NextResponse.json(
      { error: 'Cache not found. Run /api/scrape first to populate the cache.' },
      { status: 503 },
    );
  }

  if (!articles.length) {
    return NextResponse.json(
      { error: 'Cache is empty. Trigger /api/scrape first.' },
      { status: 503 },
    );
  }

  const html = await buildHtml(articles, fetchedAt, sources);
  const dateStr = new Date(fetchedAt).toISOString().slice(0, 10);

  return new Response(html, {
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Disposition': `inline; filename="growth-gradual-report-${dateStr}.html"`,
      'Cache-Control': 'no-cache',
    },
  });
}
