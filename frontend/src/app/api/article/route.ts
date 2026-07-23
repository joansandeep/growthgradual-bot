
// Added reliability patch
const ALPHA_VANTAGE_KEYS = (process.env.ALPHA_VANTAGE_API_KEYS || "")
 .split(",")
 .map(v => v.trim())
 .filter(Boolean);

let avKeyIndex = 0;
function getAlphaVantageKey() {
  if (!ALPHA_VANTAGE_KEYS.length) return "";
  const key = ALPHA_VANTAGE_KEYS[avKeyIndex];
  avKeyIndex = (avKeyIndex + 1) % ALPHA_VANTAGE_KEYS.length;
  return key;
}

import { NextRequest, NextResponse } from 'next/server';
import { parse, HTMLElement } from 'node-html-parser';
import { createLogger } from '@/lib/logger';

const log = createLogger('api/article');

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export interface ArticleContent {
  title: string;
  author: string;
  publishedAt: string;
  heroImage: string;
  heroImagePosition: 'top' | 'afterLede'; // 'top' = before first paragraph; 'afterLede' = after first paragraph
  images: string[];
  paragraphs: string[];
  source: string;
  sourceUrl: string;
  favicon: string;
  readingTime: number;
  fullContentFetched: boolean;
  // Set true when the article is behind a genuine subscription paywall (e.g.
  // BusinessLine's "READ MORE" soft-paywall) and only preview text could be
  // extracted. Optional so every other source's existing output is untouched.
  premium?: boolean;
  // Set true when the page is a live-blog (schema.org LiveBlogPosting) rather
  // than a standard NewsArticle — `paragraphs` holds chronological updates.
  isLiveBlog?: boolean;
}

// ─── "Read More" link patterns per site ──────────────────────────────────────
// Some sites split articles across multiple pages or behind a query param
interface SourceRule {
  match: RegExp;
  name: string;
  contentSelectors: string[];
  removeSelectors: string[];
  useBrSplit: boolean;
  // CSS selectors that indicate truncated content — click/follow these
  readMoreSelectors?: string[];
  // If the full article is at a predictable alternate URL, transform it
  fullUrlTransform?: (url: string) => string | null;
  // XHR/API endpoint to fetch full article text (some sites expose it)
  fullApiUrl?: (url: string) => string | null;
}

const SOURCE_RULES: SourceRule[] = [
  // ── Yahoo Finance ─────────────────────────────────────────────────────────────
  // Yahoo Finance redesigned in 2025. The article body is now inside:
  //   <div class="body yf-v6n2s3" data-testid="article-body">
  // Paragraphs use class="yf-1fy9kyt". Old caas-body selectors no longer exist.
  {
    match: /finance\.yahoo\.com/,
    name: 'Yahoo Finance',
    contentSelectors: [
      // 2025+ redesign — primary selectors
      'div[data-testid="article-body"]',
      'div.body.yf-v6n2s3',
      'div[class*="body"][class*="yf-"]',
      // article wrapper fallbacks
      'article[data-testid="article-content-wrapper"]',
      'div.body-wrap',
      // legacy caas-body (pre-2025)
      'div.caas-body',
      'div[class*="caas-body"]',
      'div[class*="article-body"]',
      'article',
    ],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript', 'iframe',
      // 2025 ad containers
      'div[data-testid="inarticle-ad"]',
      'div[class*="sdaContainer"]',
      'div[id*="google_ads"]',
      // read-more collapse wrapper (hidden content — we handle via read-more pass)
      'div.readmore',
      // legacy
      'div[class*="caas-related"]', 'div[class*="caas-readmore"]',
      'div[class*="advertisement"]', 'div.caas-figure',
      // "Most Read" link lists injected inside article
      'ul.yf-1p2hw41',
    ],
    useBrSplit: false,
  },
  // ── Investing.com ─────────────────────────────────────────────────────────────
  {
    match: /investing\.com/,
    name: 'Investing.com',
    contentSelectors: [
      'div[class*="articlePage"]', 'div#article',
      '[data-test="article-content"]', 'article',
    ],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript',
      'div[class*="relatedArticles"]', 'div[class*="advertisement"]', 'div.disclaimer',
    ],
    useBrSplit: false,
  },
  // ── Equitymaster ──────────────────────────────────────────────────────────────
  {
    match: /equitymaster\.com/,
    name: 'Equitymaster',
    contentSelectors: ['div#articletext', 'div.article-body', 'article'],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript',
      'div.related-articles', 'div[class*="subscription"]',
    ],
    useBrSplit: false,
  },
  // ── Investopedia ──────────────────────────────────────────────────────────────
  {
    match: /investopedia\.com/,
    name: 'Investopedia',
    contentSelectors: [
      'div[class*="article-body-content"]',
      'div[class*="comp mntl-sc-page"]',
      'article',
    ],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript',
      'div[class*="advertisement"]', 'div[class*="newsletter"]',
    ],
    useBrSplit: false,
  },
  // ── Economic Times ────────────────────────────────────────────────────────────
  // Verified: div.artText is the primary content div on ET article pages.
  // Print version (/printarticle/) gives full unpaywalled text.
  {
    match: /economictimes\.indiatimes\.com/,
    name: 'Economic Times',
    contentSelectors: [
      'div.artText', 'article.artData', 'div.pageContent', 'div[class*="artText"]',
    ],
    removeSelectors: [
      'div.primeSWrapper', 'div.stockpro_widget', 'div.as_freetrial_widget',
      'div.masterclass_carousel', 'div.liveEventMain_widget', 'div.adBox',
      'div[id*="mgid"]', 'div[id*="vdoai"]', 'div[id*="gpt"]',
      'div.inSideInd', 'div.artSyn', 'div.mktArticleUradar',
      'div[class*="share"]', 'div.bookMarks', 'div.fontSize',
      'div.comments', 'div.artPrint', 'aside', 'script', 'style', 'noscript',
      '.ai_podcast_030825', '.popup_ai_pb', '.ASFreeTrialWidget',
      'div.rating_block', 'div.relTopics', 'div.disclamerText',
    ],
    useBrSplit: true,
    fullUrlTransform: (url) => {
      const m = url.match(/\/articleshow\/(\d+)\.cms/);
      if (m) return url.replace(`/articleshow/${m[1]}.cms`, `/printarticle/${m[1]}.cms`);
      return null;
    },
  },
  // ── Moneycontrol ──────────────────────────────────────────────────────────────
  // Verified: div.arti-flow and div.article_content confirmed by multiple scrapers.
  {
    match: /moneycontrol\.com/,
    name: 'Moneycontrol',
    contentSelectors: [
      'div.arti-flow', 'div.article_content', 'div.artText',
      'div.article-desc', 'div[class*="article_desc"]',
      'div.content_wrapper', 'div.article-body',
      'section.article_content', 'div#article-content', 'article',
    ],
    removeSelectors: [
      'div.seo-text', 'div.relstory', 'div.tag-group', 'aside', 'script', 'style',
      'div.subscription', 'div.follow-section', 'div.advSlotsGrayBox', 'amp-ad',
      'amp-embed', 'amp-analytics', 'amp-fx-flying-carpet', 'div.strArtcl',
      'div.maintextdiv', 'div.author_wrapper', 'div.tags_wrapper',
      'div.breadcrumb', 'nav', 'header', 'footer',
      'div[class*="related"]', 'div[class*="recommend"]',
      // Moneycontrol-specific noise
      'div#readmoredivarticle',          // "Read More" expand button
      'div[id^="taboola-"]',             // Taboola recommendation feeds
      'div#mc-outstream-player',         // Outstream video player
      'div.social_icons_wrapper',        // Social share icon lists
      'div.common_allshare_icons',       // Floating share icons
      'section#video_on_hp',             // "Watch" video carousel in RHS
      'div.contact',                     // Newsletter subscribe block
      'div#firstArticle_13942471',       // Trending news sidebar widget
      'div.video-widget',                // Must Listen podcast widget
      'div.articleRHS',                  // Right-hand sidebar
      'div.page_right_wrapper',          // Entire RHS wrapper
      'div[class*="advert"]', 'div[class*="advSlot"]', 'div[class*="advHolder"]',
    ],
    useBrSplit: false,
    fullUrlTransform: (url) => {
      try {
        const cleanUrl = url.replace(/\/amp\/?(\.*)?$/, '').replace(/\?$/, '');
        const u = new URL(cleanUrl || url);
        u.searchParams.delete('amp');
        return u.toString();
      } catch { return null; }
    },
  },
  // ── Livemint ──────────────────────────────────────────────────────────────────
  // Verified: div.mainArea and div.storyContent confirmed.
  {
    match: /livemint\.com/,
    name: 'Livemint',
    contentSelectors: [
      'div.mainArea', 'div.storyContent', 'div[class*="articleBody"]',
      'div[class*="storyBody"]', 'div.paywall-container', 'article',
    ],
    removeSelectors: [
      'div.storyReco', 'div[class*="subscribe"]', 'div.also-read',
      'div[class*="advertisement"]', 'aside', 'script', 'style',
    ],
    useBrSplit: false,
  },
  // ── Business Standard ─────────────────────────────────────────────────────────
  // Primary: __NEXT_DATA__ JSON extraction (handled separately in parseArticlePage).
  // Fallback CSS: class*= patterns for the Next.js CSS module class names.
  {
  match: /business-standard\.com/,
  name: 'Business Standard',
  contentSelectors: [
    '.post-entry.story-detail',
    '.story-detail',
    '.introductory-div',
    '.content-div',
    '.content-items',
    '.lvblgcont',
    'div[class*="storydetail"]',
    'div[class*="MainStory"]',
    'div[class*="article-content"]',
    'article',
  ],
    removeSelectors: [
      'aside', 'div[class*="related"]', 'div[class*="subscribe"]',
      'div[class*="social"]', 'div[class*="advertisement"]',
      'div[class*="ads"]', 'div[class*="advert"]', 'div[class*="alsoread"]',
      'div[class*="recommend"]', 'div[class*="postcomment"]',
      'div[class*="topiclisting"]', 'div[class*="whtsppchannle"]',
      'div[class*="storymeta"]', 'div[class*="autdtl"]', 'div[class*="storyimage"]',
      'div[id*="gpt"]', 'div[id*="div-gpt"]', 'div.advertisement-bg',
      'script', 'style', 'noscript',
      'div[class*="livebutton"]',
      'div[class*="load-more"]',
      '.advertisement-bg',
      '.ads-blk',
      'iframe',
    ],
    useBrSplit: false,
  },
  // ── Financial Express ─────────────────────────────────────────────────────────
  {
    match: /financialexpress\.com/,
    name: 'Financial Express',
    contentSelectors: [
      'div#pcl-full-content', 'div.pcl-container', 'div.post-content',
      'div.ie-story-detail', 'div[class*="article-body"]',
      'div[itemprop="articleBody"]', 'div.full-details', 'article',
    ],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript',
      'div[class*="related"]', 'div[class*="also-read"]',
      'div.wp-block-ie-network-blocks-also-read',
      'div[class*="subscribe"]', 'div[class*="taboola"]',
      'div[class*="ad-code"]', 'div[class*="adcode"]',
      'div.container-wall-exclusive > div[style*="height"]',
      'figure', 'div.tablist', 'div.ie-first-publish',
      'div.article_follow_us', 'div.social-icons', 'section.stories_fe_widget',
    ],
    useBrSplit: false,
  },
  // ── NDTV Profit ───────────────────────────────────────────────────────────────
  // NDTV Profit (ndtvprofit.com) — React app.
  // div.sp-cn (story page content) is the confirmed primary wrapper.
  {
    match: /ndtv(profit)?\.com/,
    name: 'NDTV Profit',
    contentSelectors: [
      'div.sp-cn',
      'div[class*="storyPage"]',
      'div.story__content',
      'div[class*="story__content"]',
      'div[class*="story-content"]',
      'div[itemprop="articleBody"]',
      'article',
    ],
    removeSelectors: [
      'aside', 'div.related-stories', 'div[class*="related"]',
      'div[class*="advertisement"]', 'div[class*="also-read"]',
      'div[class*="social-share"]', 'div[class*="newsletter"]',
      'script', 'style', 'noscript', 'iframe',
    ],
    useBrSplit: false,
  },
  // ── The Hindu BusinessLine ────────────────────────────────────────────────────
  // Drupal CMS — div.article-body-content is the standard Drupal content div.
  {
    match: /thehindubusinessline\.com/,
    name: 'The Hindu BusinessLine',
    contentSelectors: [
      'div.article-body-content',
      'div[class*="article-body"]',
      'div[itemprop="articleBody"]',
      'div.article-content',
      'article',
    ],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript',
      'div[class*="related"]', 'div[class*="also-read"]',
      'div[class*="subscribe"]', 'div[class*="social"]',
      'div.comments', 'div.bl-premium-article-paywall',
    ],
    useBrSplit: false,
  },
  // ── Reuters ───────────────────────────────────────────────────────────────────
  // data-testid="ArticleBody" is the most stable Reuters selector.
  {
    match: /reuters\.com/,
    name: 'Reuters',
    contentSelectors: [
      'div[data-testid="ArticleBody"]',
      'div[class*="article-body__content"]',
      'div[class*="article-body"]',
      '[data-module="ArticleBody"]',
      'article',
    ],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript',
      'div[class*="related"]', 'div[class*="trustbadge"]',
      'div[data-testid="RelatedCoverage"]',
    ],
    useBrSplit: false,
  },
  // ── CNBC TV18 ─────────────────────────────────────────────────────────────────
  // Verified from live HTML (June 2026):
  //   article#art-entry-wrap
  //     └─ div.narticle-text
  //          └─ div.outblurdiv.narticle-data        ← real content wrapper
  //               └─ div.articleWrap               ← paragraphs (br-separated)
  //
  // IMPORTANT: narticle-data MUST NOT be removed — it wraps all article text.
  // The hidden Taboola section sits inside div.d-none and must be stripped.
  // useBrSplit: true is correct — content uses <br><br> between paragraphs.
  {
    match: /cnbctv18\.com/,
    name: 'CNBC TV18',
    contentSelectors: [
      'div.outblurdiv.narticle-data',   // most specific — confirmed present
      'div[class*="outblurdiv"]',
      'div.articleWrap',
      'div.narticle-text',
      'section.new-article',
      'article#art-entry-wrap',
      'article',
    ],
    removeSelectors: [
      // Ads
      'div.adBox', 'div[class*="adBox"]', 'div[class*="adunitContainer"]',
      'div[id*="google_ads"]', 'div[class*="advertisement"]',
      'div[class*="ad_cntainer"]', 'div[class*="flying-carpet"]',
      // Taboola / "Suggested for you" — always inside div.d-none
      'div.d-none', 'div[class*="taboola"]', 'div[id*="taboola"]',
      // Share / social / author blocks above the text
      'div.narticle-share', 'div.narticle-photo', 'div.narticle-summary',
      'div.narticle-author', 'div.narticle-date-share', 'div.bookmark-div',
      // "Continue Reading" button and the collapsed footer section
      'div.btn-sec', 'div.contentdiv',
      // Generic noise
      'div[class*="related"]', 'div[class*="also-read"]',
      'script', 'style', 'noscript', 'iframe',
      // Lazy-load placeholders (they add spurious height text)
      'div.lazyload-wrapper', 'div.lazyload-placeholder',
    ],
    useBrSplit: true,
  },
  // ── Zee Business ──────────────────────────────────────────────────────────────
  // Verified by fetching live article HTML: clean structured HTML with standard
  // h2/p/li inside article tag. itemprop="articleBody" is the schema.org wrapper.
  {
    match: /zeebiz\.com/,
    name: 'Zee Business',
    contentSelectors: [
      'div[itemprop="articleBody"]',
      'div[class*="article-content"]',
      'div[class*="article-body"]',
      'div[class*="content-area"]',
      'article',
    ],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript', 'iframe',
      'div[class*="related"]', 'div[class*="also-read"]',
      'div[class*="advertisement"]', 'div[class*="social"]',
      'div[class*="subscribe"]', 'div[class*="newsletter"]',
      'div[class*="tags"]', 'div[class*="author"]',
    ],
    useBrSplit: false,
  },
  // ── Dalal Street Investment Journal (DSIJ) ────────────────────────────────
  // Drupal-based CMS. article.node--type-article is the standard Drupal content
  // container. The session_id cookie (from DSIJ_COOKIES env) unlocks premium content;
  // free/logged-out articles still render the lede without it.
  {
    match: /dsij\.in/,
    name: 'Dalal Street Journal',
    contentSelectors: [
      'article.node--type-article',
      'div.field--name-body',
      'div[class*="article-body"]',
      'div.node__content',
      'div[itemprop="articleBody"]',
      'article',
    ],
    removeSelectors: [
      'aside', 'script', 'style', 'noscript', 'iframe',
      'div.field--name-field-tags', 'div[class*="social-share"]',
      'div[class*="related"]', 'div[class*="also-read"]',
      'div[class*="advertisement"]', 'div[class*="subscribe"]',
      'div.block-views', 'div[class*="comments"]',
      'div[class*="author"]', 'div[class*="breadcrumb"]',
      'nav', 'header', 'footer',
    ],
    useBrSplit: false,
  },
];


const GENERIC_SELECTORS = [
  '[itemprop="articleBody"]', 'article',
  'div[class*="article-body"]', 'div[class*="story-body"]',
  'div[class*="post-content"]', 'div[class*="entry-content"]',
  'div[class*="content-body"]', 'main',
];

// ─── "Read More" / pagination detectors ──────────────────────────────────────
const READ_MORE_SELECTORS = [
  'a.read-more', 'a[class*="read-more"]', 'a[class*="readmore"]',
  'button.read-more', 'span.read-more', 'div.read-more a',
  'a.show-more', 'a[class*="show-more"]',
  'a.load-more', 'a[class*="load-more"]',
  'a.article-more', 'a[class*="full-story"]',
  'a[rel="next"]',                        // pagination
  'li.next a', 'a.next-page',
  'a[href*="?page=2"]', 'a[href*="/2/"]',
];

// Patterns in anchor text that mean "expand / read full"
const READ_MORE_TEXT_RE = /\b(read more|read full|show more|full article|expand|continue reading|load more|next page)\b/i;

// ─── Utilities ────────────────────────────────────────────────────────────────
function absoluteUrl(src: string, base: string): string {
  if (!src) return '';
  src = src.trim();
  if (src.startsWith('data:')) return '';
  if (src.startsWith('//')) return 'https:' + src;
  if (src.startsWith('http')) return src;
  try { return new URL(src, base).href; } catch { return ''; }
}

function isUsableImage(src: string): boolean {
  if (!src || src.length < 12) return false;
  if (/\/(pixel|tracking|beacon|ads?|logo|icon|favicon|sprite|blank|placeholder|spacer|1x1|avatar)\b/i.test(src)) return false;
  if (/\.(svg|ico)(\?|$)/i.test(src)) return false;
  // Filter tiny images (width <= 100 in URL) unless it's an etimg CDN URL
  if (/width[-_]?(\d+)/.test(src)) {
    const w = parseInt(src.match(/width[-_]?(\d+)/)?.[1] ?? '999');
    if (w < 200 && !/etimg\.com/.test(src)) return false;
  }
  return /\.(jpg|jpeg|png|webp)(\?|$)/i.test(src) ||
    /etimg\.com|images\.(livemint|businessstandard)|cloudfront|s3\.amazonaws/i.test(src);
}

const ENTITY_MAP: Record<string, string> = {
  '&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'",'&nbsp;':' ',
  '&rsquo;':'\u2019','&lsquo;':'\u2018','&ldquo;':'\u201c','&rdquo;':'\u201d',
  '&hellip;':'\u2026','&mdash;':'\u2014','&ndash;':'\u2013',
  '&#8216;':'\u2018','&#8217;':'\u2019','&#8220;':'\u201c','&#8221;':'\u201d',
  '&#8230;':'\u2026','&#8212;':'\u2014',
};
function decodeEntities(s: string): string {
  return s.replace(/&[a-z#0-9]+;/gi, m => ENTITY_MAP[m] ?? m);
}
function cleanText(t: string): string {
  return decodeEntities(t.replace(/\s+/g, ' ').trim());
}
function estimateReadingTime(paras: string[]): number {
  return Math.max(1, Math.round(paras.join(' ').split(/\s+/).length / 200));
}

// ─── Fetch with realistic browser headers ─────────────────────────────────────
async function fetchHTML(url: string, referer?: string): Promise<string> {
  const headers: Record<string, string> = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-IN,en;q=0.9,hi;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': referer ? 'same-origin' : 'none',
    'Upgrade-Insecure-Requests': '1',
  };
  if (referer) headers['Referer'] = referer;

  // Inject session cookies for login-gated sources
  // DSIJ requires a valid session_id cookie to access premium article content
  if (/dsij\.in/i.test(url) && process.env.DSIJ_COOKIES) {
    headers['Cookie'] = process.env.DSIJ_COOKIES;
  }

  const res = await fetch(url, { headers, signal: AbortSignal.timeout(14000) });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res.text();
}

// ─── Find "read more" links in the parsed page ────────────────────────────────
function findReadMoreLinks(root: ReturnType<typeof parse>, pageUrl: string): string[] {
  const found: string[] = [];

  // 1. Check known selectors
  for (const sel of READ_MORE_SELECTORS) {
    try {
      root.querySelectorAll(sel).forEach(el => {
        const href = el.getAttribute('href');
        if (href) {
          const abs = absoluteUrl(href, pageUrl);
          if (abs && abs !== pageUrl && !found.includes(abs)) found.push(abs);
        }
      });
    } catch { /* skip */ }
  }

  // 2. Scan ALL anchors for "read more"-like text
  root.querySelectorAll('a').forEach(el => {
    const text = el.text?.trim() || '';
    const href = el.getAttribute('href') || '';
    if (READ_MORE_TEXT_RE.test(text) && href) {
      const abs = absoluteUrl(href, pageUrl);
      if (abs && abs !== pageUrl && !found.includes(abs)) found.push(abs);
    }
  });

  // 3. ET-specific: "printarticle" or "full-article" style links in page
  root.querySelectorAll('a[href*="printarticle"]').forEach(el => {
    const abs = absoluteUrl(el.getAttribute('href') || '', pageUrl);
    if (abs && !found.includes(abs)) found.push(abs);
  });

  return found.slice(0, 3); // max 3 candidates
}

// ─── Check if content looks truncated ────────────────────────────────────────
function isTruncated(paragraphs: string[], html: string): boolean {
  const wordCount = paragraphs.join(' ').split(/\s+/).length;
  // Fewer than 80 words is almost certainly truncated
  if (wordCount < 60) return true;
  // Common paywall / truncation signals in the HTML
  if (/paywall|subscribe to (read|continue|unlock)|premium content|members only/i.test(html)) return true;
  // ET's "show more" div
  if (/class="hide_content|class="disc_ellipsis/i.test(html) && wordCount < 200) return true;
  return false;
}

// ─── Meta extraction ──────────────────────────────────────────────────────────
function extractMeta(root: ReturnType<typeof parse>, pageUrl: string) {
  const getMeta = (...names: string[]) => {
    for (const n of names) {
      const el = root.querySelector(`meta[property="${n}"]`) ?? root.querySelector(`meta[name="${n}"]`);
      const c = el?.getAttribute('content');
      if (c) return cleanText(c);
    }
    return '';
  };

  const title = cleanText(
    getMeta('og:title','twitter:title') ||
    root.querySelector('.topPart h1.artTitle')?.text ||
    root.querySelector('h1.article_title')?.text ||
    root.querySelector('h1.artTitle')?.text ||
    root.querySelector('h1')?.text ||
    root.querySelector('title')?.text || ''
  );

  const author = getMeta('author','article:author','og:article:author','twitter:creator','dc.creator');

  let publishedAt = getMeta('article:published_time','datePublished','og:article:published_time','pubdate','date');
  if (!publishedAt) {
    const timeEl = root.querySelector('time.jsdtTime') ?? root.querySelector('time[datetime]');
    publishedAt = timeEl?.getAttribute('datetime') || timeEl?.text || '';
  }
  let formattedDate = '';
  if (publishedAt) {
    try {
      formattedDate = new Date(publishedAt).toLocaleDateString('en-IN', {
        day:'numeric', month:'long', year:'numeric', hour:'2-digit', minute:'2-digit'
      });
    } catch { formattedDate = publishedAt; }
  }

  const heroImage = getMeta('og:image','twitter:image:src','twitter:image','og:image:secure_url');

  const favicon = (() => {
    const el = root.querySelector('link[rel="icon"]') ?? root.querySelector('link[rel="shortcut icon"]') ?? root.querySelector('link[rel="apple-touch-icon"]');
    return absoluteUrl(el?.getAttribute('href') || '/favicon.ico', pageUrl);
  })();

  return { title, author, publishedAt: formattedDate, heroImage, favicon };
}

// ─── Image extraction ─────────────────────────────────────────────────────────
// ─── Detect whether the first image appears before or after the first paragraph ─
function detectHeroImagePosition(container: HTMLElement): 'top' | 'afterLede' {
  // Walk all direct and nested children in DOM order; find which comes first: img or p
  const walker = container.querySelectorAll('img, p, h2, h3, blockquote');
  for (const el of walker) {
    const tag = el.tagName?.toLowerCase();
    if (tag === 'img') {
      const src = el.getAttribute('data-src') || el.getAttribute('data-lazy-src') ||
                  el.getAttribute('data-original') || el.getAttribute('src') || '';
      if (src && isUsableImage(src)) return 'top'; // image comes before any paragraph
    }
    if (tag === 'p' || tag === 'h2' || tag === 'h3' || tag === 'blockquote') {
      const text = (el.text || '').trim();
      if (text.length > 30) return 'afterLede'; // paragraph comes first
    }
  }
  return 'afterLede'; // default
}

function extractImages(container: HTMLElement, pageUrl: string): string[] {
  const seen = new Set<string>();
  const images: string[] = [];
  for (const img of container.querySelectorAll('img')) {
    const src = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') ||
                img.getAttribute('data-original') || img.getAttribute('src') || '';
    const abs = absoluteUrl(src, pageUrl);
    if (isUsableImage(abs) && !seen.has(abs)) { images.push(abs); seen.add(abs); }
  }
  for (const src of container.querySelectorAll('source')) {
    const first = (src.getAttribute('srcset') || '').split(',')[0]?.split(' ')[0]?.trim() || '';
    const abs = absoluteUrl(first, pageUrl);
    if (isUsableImage(abs) && !seen.has(abs)) { images.push(abs); seen.add(abs); }
  }
  return images;
}

// ─── Paragraph extraction ─────────────────────────────────────────────────────
const SKIP_RE = /subscribe|sign up|cookie|newsletter|advertisement|login to read|etprime|prime member|whatsapp channel|follow us|ET now|download the app|get app|join us/i;
const CSS_NOISE_RE = /^\s*[.#\[][\w-]+\s*\{/;

function extractParagraphs(container: HTMLElement, useBrSplit: boolean): string[] {
  const paragraphs: string[] = [];

  if (useBrSplit) {
    const html = container.innerHTML;
    const chunks = html
      .replace(/<br\s*\/?>\s*<br\s*\/?>/gi, '\n\n')
      .replace(/<br\s*\/?>/gi, '\n')
      .split(/\n{2,}/);

    for (const chunk of chunks) {
      const text = cleanText(chunk.replace(/<[^>]+>/g, ' '));
      if (text.length < 40) continue;
      if (SKIP_RE.test(text) && text.length < 180) continue;
      if (CSS_NOISE_RE.test(text)) continue;
      if (text.includes('{') && text.includes(':') && text.includes('}') && text.length < 400) continue;
      paragraphs.push(text);
    }

    // Include h2/h3 headings as section markers
    for (const h of container.querySelectorAll('h2, h3')) {
      const t = cleanText(h.text);
      if (t.length > 4 && !paragraphs.some(p => p.includes(t))) {
        paragraphs.push('§ ' + t);
      }
    }
    return paragraphs;
  }

  // Standard: <p> tags
  for (const el of container.querySelectorAll(
  'p, h2, h3, h4, blockquote, li'
)) {
  const text = cleanText(el.text);

  if (text.length < 20) continue;
  if (SKIP_RE.test(text) && text.length < 180) continue;

  paragraphs.push(text);
}

// Business Standard live blogs
for (const el of container.querySelectorAll(
  '.lvblgcont,.content-items'
)) {
  const text = cleanText(el.text);

  if (
    text.length > 30 &&
    !paragraphs.includes(text)
  ) {
    paragraphs.push(text);
  }
}

  // Fallback: raw text split
  if (paragraphs.length < 3) {
    const raw = cleanText(container.text);
    const sentences = raw.split(/(?<=[.!?])\s+(?=[A-Z"'])/);
    let chunk = '';
    for (const s of sentences) {
      chunk += s + ' ';
      if (chunk.length > 220) {
        const t = chunk.trim();
        if (t.length > 40 && !SKIP_RE.test(t)) paragraphs.push(t);
        chunk = '';
      }
    }
    if (chunk.trim().length > 40) paragraphs.push(chunk.trim());
  }

  return paragraphs;
}

// ─── Business Standard: extract from __NEXT_DATA__ JSON ──────────────────────
// BS is a Next.js app. The full article HTML is embedded in a <script id="__NEXT_DATA__">
// JSON blob as `data.htmlContent`. This is far more reliable than DOM scraping because
// it's clean server-rendered content with no ads, paywalls, or CSS module class names.
function extractBSNextData(html: string): { paragraphs: string[]; title: string; author: string; publishedAt: string; heroImage: string } | null {
  try {
    const match = html.match(/<script[^>]+id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/i);
    if (!match?.[1]) return null;

    const json = JSON.parse(match[1]);
    const data = json?.props?.pageProps?.data;
    if (!data) return null;

    const htmlContent: string = data.htmlContent || '';
    const title: string = cleanText(data.heading1 || data.pageTitle || '');
    const author: string = (() => {
      const authors = data.articleMappedMultipleAuthors;
      if (authors && typeof authors === 'object') {
        const names = Object.values(authors) as string[];
        if (names.length) return names.join(', ');
      }
      return '';
    })();
    let publishedAt = '';
    if (data.publishDate) {
      try {
        publishedAt = new Date(parseInt(data.publishDate) * 1000).toLocaleDateString('en-IN', {
          day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit',
        });
      } catch { publishedAt = ''; }
    }
    const heroImage: string =
  data.articleContentImage ||
  data.image ||
  data.imageUrl ||
  '';

    if (!htmlContent) return null;

    // Parse the embedded HTML content into paragraphs
    const contentRoot = parse(htmlContent, { lowerCaseTagName: false, comment: false });
    contentRoot.querySelectorAll('script, style, noscript, iframe, div[id*="gpt"], div[class*="advert"], div[class*="advertisement"]').forEach(el => el.remove());

    const paragraphs: string[] = [];
    for (const el of contentRoot.querySelectorAll('p, h2, h3, h4, blockquote')) {
      const text = cleanText(el.text);
      if (text.length < 20) continue;
      if (SKIP_RE.test(text) && text.length < 180) continue;
      paragraphs.push(text);
    }

    // Fallback: raw text if no <p> tags found
    if (paragraphs.length < 2) {
      const raw = cleanText(contentRoot.text);
      if (raw.length > 100) {
        const sentences = raw.split(/(?<=[.!?])\s+(?=[A-Z"'])/);
        let chunk = '';
        for (const s of sentences) {
          chunk += s + ' ';
          if (chunk.length > 200) {
            const t = chunk.trim();
            if (t.length > 40 && !SKIP_RE.test(t)) paragraphs.push(t);
            chunk = '';
          }
        }
        if (chunk.trim().length > 40) paragraphs.push(chunk.trim());
      }
    }

    if (paragraphs.length === 0) return null;
    return { paragraphs, title, author, publishedAt, heroImage };
  } catch (e) {
    log.warn('__NEXT_DATA__ parse failed: %s', e);
    return null;
  }
}

// ─── Core: fetch + parse one URL ─────────────────────────────────────────────
function parseArticlePage(
  html: string,
  url: string,
  rule: SourceRule | undefined
) {

  // ===========================
  // Yahoo Finance Special Handling (2025+ redesign)
  // ===========================
  // Yahoo Finance collapses the second half of every article into a
  // <div class="read-more-wrapper" style="display:none"> behind a JS button.
  // Since we fetch raw HTML (no JS execution), we need to manually unhide it
  // by removing the inline style before handing to the generic parser.
  // Also strip ad containers early so they don't pollute paragraph extraction.
  if (/finance\.yahoo\.com/i.test(url)) {
    // 1. Un-hide the collapsed "read-more-wrapper" section
    html = html.replace(
      /(<div[^>]+class="[^"]*read-more-wrapper[^"]*"[^>]*)\s+style="display:\s*none[^"]*"/gi,
      '$1'
    );
    // 2. Remove in-article ad iframes / placeholders (noisy text if kept)
    html = html.replace(/<div[^>]+data-testid="inarticle-ad"[^>]*>[\s\S]*?<\/div>\s*<\/div>/gi, '');
  }

  // ===========================
  // CNBC TV18 Special Handling
  // ===========================
  // CNBCTV18 embeds a large Taboola "Suggested For You" carousel inside the
  // article body in a div.d-none (hidden via CSS class, not inline style).
  // node-html-parser sometimes struggles to remove deeply nested hidden divs
  // via CSS selectors, so we strip the entire Taboola block at the HTML level
  // before parsing. Also remove lazyload placeholders that inject height text.
  if (/cnbctv18\.com/i.test(url)) {
    // Strip Taboola carousel block
    html = html.replace(/<div[^>]+class="[^"]*taboola[^"]*"[^>]*>[\s\S]*?<\/div>\s*<\/div>\s*<\/div>\s*<\/div>\s*<\/div>\s*<\/div>/gi, '');
    // Strip lazy-load placeholder divs (they contain no text but add noise)
    html = html.replace(/<div[^>]+class="[^"]*lazyload-placeholder[^"]*"[^>]*>[\s\S]*?<\/div>/gi, '');
    // Strip the "Continue Reading" button section and the collapsed footer
    html = html.replace(/<div[^>]+class="[^"]*btn-sec[^"]*"[^>]*>[\s\S]*?<\/div>/gi, '');
    html = html.replace(/<div[^>]+class="[^"]*contentdiv[^"]*d-none[^"]*"[^>]*>[\s\S]*?<\/div>/gi, '');
  }

  // ===========================
  // Moneycontrol Special Handling
  // ===========================
  // MC gates the bottom half of articles behind a JS "Read More" button by
  // setting style="display: none;" on individual <p> tags. Since we fetch raw
  // HTML with no JS execution we must strip those inline styles so the
  // paragraphs are treated as visible content by the generic parser.
  //
  // Also strip the noisy Taboola feed, outstream video player, social share
  // blocks, and the "Read More" button div before parsing.
  if (/moneycontrol\.com/i.test(url)) {
    // 1. Un-hide paragraphs collapsed by the "Read More" JS toggle
    html = html.replace(/<p([^>]*)\sstyle="[^"]*display\s*:\s*none[^"]*"([^>]*)>/gi, '<p$1$2>');
    // 2. Strip the "Read More" expand button div
    html = html.replace(/<div[^>]+id="readmoredivarticle"[^>]*>[\s\S]*?<\/div>/gi, '');
    // 3. Strip Taboola recommendation blocks (large, nested, pollute paragraph extraction)
    html = html.replace(/<div[^>]+id="taboola-[^"]*"[^>]*>[\s\S]*?<\/div>\s*<\/div>\s*<\/div>\s*<\/div>\s*<\/div>/gi, '');
    // 4. Strip the outstream video player div (lots of noise)
    html = html.replace(/<div[^>]+id="mc-outstream-player"[^>]*>[\s\S]*?<\/div>\s*<\/div>/gi, '');
    // 5. Strip social share icon lists
    html = html.replace(/<div[^>]+class="[^"]*social_icons_wrapper[^"]*"[^>]*>[\s\S]*?<\/div>/gi, '');
    // 6. Strip the boilerplate "Discover Business News..." footer paragraph
    html = html.replace(/<div[^>]+class="[^"]*maintextdiv[^"]*"[^>]*>[\s\S]*?<\/div>/gi, '');
  }

  // ===========================
  // Business Standard Special Handling
  // ===========================

  if (/business-standard\.com/i.test(url)) {

    // Try __NEXT_DATA__ first
    const bsData = extractBSNextData(html);

    if (bsData && bsData.paragraphs.length > 0) {

      const rootForMeta = parse(html, {
        lowerCaseTagName: false,
        comment: false
      });

      const meta = extractMeta(rootForMeta, url);

      return {
        meta: {
          title: bsData.title || meta.title,
          author: bsData.author || meta.author,
          publishedAt: bsData.publishedAt || meta.publishedAt,
          heroImage: bsData.heroImage || meta.heroImage,
          favicon: meta.favicon,
        },
        images: bsData.heroImage
          ? [bsData.heroImage]
          : (meta.heroImage ? [meta.heroImage] : []),
        paragraphs: bsData.paragraphs,
        readMoreLinks: [],
        rawHtml: html,
        container: null as unknown as HTMLElement,
        heroImagePosition: 'afterLede' as const,
        premium: false,
        isLiveBlog: false,
      };
    }

    // =====================================
    // Business Standard Live Blog Fallback
    // =====================================

    const liveRoot = parse(html, {
      lowerCaseTagName: false,
      comment: false
    });

    const liveBlocks = liveRoot.querySelectorAll(
      '.content-items, .lvblgcont'
    );

    if (liveBlocks.length > 3) {

      const paragraphs: string[] = [];

      for (const block of liveBlocks) {

        const heading =
          cleanText(
            block.querySelector(
              '.lvblghdng'
            )?.text || ''
          );

        const content =
          cleanText(
            block.querySelector(
              '.lvblgcont'
            )?.text || ''
          );

        const combined =
          [heading, content]
            .filter(Boolean)
            .join('\n');

        if (combined.length > 30) {
          paragraphs.push(combined);
        }
      }

      const meta = extractMeta(liveRoot, url);

      const heroImage =
        liveRoot
          .querySelector(
            '.MainStory_storyimage__qkAq3 img'
          )
          ?.getAttribute('src') || '';

      if (paragraphs.length > 0) {

        return {
          meta: {
            ...meta,
            heroImage: heroImage || meta.heroImage,
          },
          images: heroImage
  ? [absoluteUrl(heroImage, url)]
  : extractImages(liveRoot as any, url),
          paragraphs,
          readMoreLinks: [],
          rawHtml: html,
          container: liveRoot as unknown as HTMLElement,
          heroImagePosition: 'afterLede' as const,
          premium: false,
          isLiveBlog: true,
        };
      }
    }
  }

  // ===========================
  // The Hindu BusinessLine Special Handling
  // ===========================
  // BusinessLine (Drupal CMS) serves three distinct page shapes the generic
  // NewsArticle parser further below can't handle correctly:
  //
  //   1. Google News interstitial shells ("This article is hosted on
  //      BusinessLine" / "OPEN ON BUSINESSLINE") — these are unwrapped
  //      *before* we ever get here, in scrapeArticle(), since they arrive
  //      under a different URL/HTML than the real article. Nothing to do
  //      in this function for that case.
  //   2. Live blog pages — itemtype="http://schema.org/LiveBlogPosting"
  //      instead of the usual itemtype="http://schema.org/NewsArticle".
  //      Content lives in a series of timestamped update blocks, not one
  //      article body.
  //   3. Soft-paywalled "premium" articles — a few free paragraphs followed
  //      by a gradient overlay + red "READ MORE" CTA
  //      (div.bl-premium-article-paywall). Since we fetch static HTML with
  //      no JS execution, "clicking" the CTA means following its href via
  //      the existing read-more pass in scrapeArticle(); if that doesn't
  //      surface more text, the article is genuinely subscriber-only and we
  //      return `premium: true` with whatever preview text is available
  //      instead of letting the scrape come back empty.
  if (/thehindubusinessline\.com/i.test(url)) {
    const blRoot = parse(html, { lowerCaseTagName: false, comment: false });

    // ── Case 3: live blog detection ──────────────────────────────────────
    const isLiveBlog =
      /itemtype\s*=\s*["']https?:\/\/schema\.org\/LiveBlogPosting["']/i.test(html) ||
      blRoot.querySelector('[itemtype*="LiveBlogPosting"]') !== null;

    if (isLiveBlog) {
      const liveContainer =
        (blRoot.querySelector('[itemtype*="LiveBlogPosting"]') as HTMLElement) ?? blRoot;

      // Each update is normally itemprop="liveBlogUpdate" wrapping a
      // BlogPosting. Fall back to BusinessLine's common live-blog class
      // names if a given update is missing full schema markup.
      let updateEls = liveContainer.querySelectorAll(
        '[itemprop="liveBlogUpdate"], [itemtype*="BlogPosting"]'
      );
      if (updateEls.length === 0) {
        updateEls = liveContainer.querySelectorAll(
          '.live-blog-post, .lb-update, .live-update, .blog-post'
        );
      }

      const updates: { time: string; text: string; sortKey: number }[] = [];
      updateEls.forEach((el, i) => {
        const timeEl = el.querySelector('[itemprop="datePublished"], time');
        const isoTime =
          timeEl?.getAttribute('datetime') || timeEl?.getAttribute('content') || '';
        const displayTime = cleanText(timeEl?.text || isoTime || '');

        const bodyEl = el.querySelector(
          '[itemprop="articleBody"], [itemprop="description"], .lb-update-body, p'
        );
        const text = cleanText(bodyEl?.text || el.text || '');

        if (text.length > 15) {
          const parsedTime = isoTime ? Date.parse(isoTime) : NaN;
          updates.push({ time: displayTime, text, sortKey: Number.isNaN(parsedTime) ? i : parsedTime });
        }
      });

      // BusinessLine renders live updates newest-first in the DOM. If we
      // have real parsed timestamps, sort ascending for true chronological
      // order; otherwise fall back to reversing DOM order.
      const havePublishTimes = updateEls.length > 0 && updates.some(u => u.sortKey > updates.length);
      const ordered = havePublishTimes
        ? [...updates].sort((a, b) => a.sortKey - b.sortKey)
        : [...updates].reverse();

      const paragraphs = ordered.map(u => (u.time ? `[${u.time}] ${u.text}` : u.text));

      if (paragraphs.length > 0) {
        const meta = extractMeta(blRoot, url);
        return {
          meta,
          images: extractImages(liveContainer, url),
          paragraphs,
          readMoreLinks: [],
          rawHtml: html,
          container: liveContainer,
          heroImagePosition: 'afterLede' as const,
          premium: false,
          isLiveBlog: true,
        };
      }
      // Schema detected but no updates extracted — fall through to the
      // generic parser below rather than returning an empty article.
    }

    // ── Case 2: soft-paywall / premium detection ─────────────────────────
    const paywallEl = blRoot.querySelector(
      'div.bl-premium-article-paywall, [class*="premium-article-paywall"]'
    );
    if (paywallEl) {
      let previewContainer: HTMLElement | null = null;
      for (const sel of [
        'div.article-body-content',
        'div[class*="article-body"]',
        'div[itemprop="articleBody"]',
        'article',
      ]) {
        const el = blRoot.querySelector(sel);
        if (el) { previewContainer = el as HTMLElement; break; }
      }
      previewContainer = previewContainer ?? (blRoot as unknown as HTMLElement);

      // "Click" the READ MORE CTA — follow its href if it points anywhere
      // other than the current page. scrapeArticle()'s existing read-more
      // pass fetches this and re-runs parseArticlePage on the result; if the
      // article is truly subscriber-only, that follow-up will hit this same
      // paywall branch again and scrapeArticle() will keep `premium: true`.
      const ctaLink = paywallEl.querySelector('a');
      const ctaHref = ctaLink?.getAttribute('href') || '';
      const readMoreLinks = ctaHref ? [absoluteUrl(ctaHref, url)] : [];

      // Remove the paywall CTA block from the preview so its button label
      // ("READ MORE") doesn't leak into the extracted preview paragraphs.
      paywallEl.remove();

      const previewParagraphs = extractParagraphs(previewContainer, false);
      const meta = extractMeta(blRoot, url);

      return {
        meta,
        images: extractImages(previewContainer, url),
        paragraphs: previewParagraphs,
        readMoreLinks,
        rawHtml: html,
        container: previewContainer,
        heroImagePosition: detectHeroImagePosition(previewContainer),
        premium: true,
        isLiveBlog: false,
      };
    }
    // No paywall marker, not a live blog — fall through to the generic
    // parser below, which already has BusinessLine's contentSelectors /
    // removeSelectors registered in SOURCE_RULES for the normal case.
  }

  // ===========================
  // Generic Parser
  // ===========================

  const root = parse(html, {
    lowerCaseTagName: false,
    comment: false
  });

  root.querySelectorAll(
    'script, style, noscript, iframe'
  ).forEach(el => el.remove());

  const meta = extractMeta(root, url);

  const selectors =
    rule?.contentSelectors ?? GENERIC_SELECTORS;

  let container: HTMLElement | null = null;

  for (const sel of selectors) {
    try {
      const el = root.querySelector(sel);
      if (el) {
        container = el;
        break;
      }
    } catch {}
  }

  if (!container) {
    container =
      root.querySelector('body') as HTMLElement ??
      root as unknown as HTMLElement;
  }

  for (
    const sel of (
      rule?.removeSelectors ??
      ['aside', 'nav', 'footer']
    )
  ) {
    try {
      container
        .querySelectorAll(sel)
        .forEach(el => el.remove());
    } catch {}
  }

  // Extra ad cleanup
  container
    .querySelectorAll(
      '.advertisement-bg,.ads-blk,iframe'
    )
    .forEach(el => el.remove());

  const readMoreLinks =
    findReadMoreLinks(root, url);

  const images =
    extractImages(container, url);

  const paragraphs =
    extractParagraphs(
      container,
      rule?.useBrSplit ?? false
    );

  const heroImagePosition =
    detectHeroImagePosition(container);

  return {
    meta,
    images,
    paragraphs,
    readMoreLinks,
    rawHtml: html,
    container,
    heroImagePosition,
    premium: false,
    isLiveBlog: false,
  };
}

// ─── Main scraper: two-pass with read-more following ─────────────────────────
async function scrapeArticle(articleUrl: string): Promise<ArticleContent> {
  // Guard: reject URLs that are clearly not real article pages
  // This catches cases where Google News URL resolution returns an image CDN URL
  // (e.g. lh3.googleusercontent.com) or other non-article resources.
  const urlObj = new URL(articleUrl);
  const isGoogleInfra = GOOGLE_DOMAINS.test(urlObj.hostname);
  const isAsset = /\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|woff|woff2|ttf|pdf|mp4|mp3|zip)(\?|$)/i.test(urlObj.pathname);

  if (isGoogleInfra || isAsset) {
    // BusinessLine Case 1: before giving up, try unwrapping a Google
    // AMP-Viewer shell URL (google.com/amp/s/<real-url>) — the real
    // publisher URL is embedded right in the path, no extra fetch needed.
    const unwrapped = !isAsset ? unwrapAmpViewerUrl(articleUrl) : null;
    if (unwrapped && unwrapped !== articleUrl) {
      return scrapeArticle(unwrapped);
    }

    // Return an empty article so the UI shows the "open externally" fallback.
    // Pass an empty title so the article page falls back to the title from URL params.
    const source = urlObj.hostname.replace(/^www\./, '');
    return {
      title: '',
      author: '', publishedAt: '', heroImage: '', heroImagePosition: 'afterLede',
      images: [], paragraphs: [], source, sourceUrl: articleUrl,
      favicon: '', readingTime: 1, fullContentFetched: false,
      premium: false, isLiveBlog: false,
    };
  }

  // Strip /amp suffix globally before matching rules — Google News often serves AMP URLs
  // but the canonical non-AMP URL has better content selectors and no paywall fragments.
  let canonicalUrl = articleUrl;
  if (urlObj.pathname.endsWith('/amp') || urlObj.pathname.endsWith('/amp/')) {
    canonicalUrl = articleUrl.replace(/\/amp\/?$/, '');
    if (canonicalUrl !== articleUrl) {
      try { new URL(canonicalUrl); } catch { canonicalUrl = articleUrl; }
    }
  }
  // Use canonical URL for rule matching and scraping (fall back to original if fetch fails)
  const effectiveUrl = canonicalUrl !== articleUrl ? canonicalUrl : articleUrl;

  const rule = SOURCE_RULES.find(r => r.match.test(effectiveUrl));
  const origin = new URL(effectiveUrl).origin;

  // === PASS 1: Try the full-text URL transform first (e.g. ET print version) ===
  let primaryUrl = effectiveUrl;
  let primaryHtml: string;

  if (rule?.fullUrlTransform) {
    const transformed = rule.fullUrlTransform(effectiveUrl);
    if (transformed) {
      try {
        primaryHtml = await fetchHTML(transformed, effectiveUrl);
        primaryUrl = transformed;
      } catch {
        primaryHtml = await fetchHTML(effectiveUrl);
        primaryUrl = effectiveUrl;
      }
    } else {
      primaryHtml = await fetchHTML(effectiveUrl);
    }
  } else {
    primaryHtml = await fetchHTML(effectiveUrl);
  }

  // BusinessLine Case 1 (defense in depth): even after URL-level unwrapping
  // above, a Google interstitial shell can still arrive here under a URL
  // that didn't look Google-owned (e.g. after a redirect chain). Detect the
  // "This article is hosted on X / OPEN ON X" shell by its content and hop
  // to the real article before parsing — generic, so it protects every
  // source reached via a Google News link, not just BusinessLine. Looped
  // (bounded) since the resolved link can itself occasionally be another
  // shell before landing on the real publisher page.
  for (let hop = 0; hop < MAX_GOOGLE_REDIRECT_DEPTH; hop++) {
    const interstitialLink = await extractHostedOnInterstitialLink(primaryHtml, primaryUrl);
    if (!interstitialLink || interstitialLink === primaryUrl) break;
    try {
      const label = /thehindubusinessline\.com/i.test(interstitialLink)
        ? 'BusinessLine'
        : safeHostname(interstitialLink);
      log.info('Fetching %s article: %s', label, interstitialLink);
      primaryHtml = await fetchHTML(interstitialLink, primaryUrl);
      primaryUrl = interstitialLink;
    } catch (e) {
      log.warn('Interstitial follow-through failed: %s — %s', interstitialLink, e);
      break;
    }
  }

  const pass1 = parseArticlePage(primaryHtml, primaryUrl, rule);

  // Collect what we have
  let paragraphs = pass1.paragraphs;
  let images = pass1.images;
  let fullContentFetched = !isTruncated(paragraphs, primaryHtml);
  const { meta, heroImagePosition } = pass1;
  // BusinessLine Case 2/3: carried through from parseArticlePage. `premiumFlag`
  // can be revised by pass 2 below (e.g. if following the READ MORE CTA lands
  // on the same paywall again, or conversely on the full un-gated content).
  let premiumFlag = pass1.premium ?? false;
  const isLiveBlog = pass1.isLiveBlog ?? false;

  // === PASS 2: Follow read-more links if content is still truncated ===
  // (For BusinessLine premium articles, pass1.readMoreLinks holds the
  // "READ MORE" CTA href — this is our "click" in a no-JS/static-fetch
  // architecture: we follow the link instead of simulating a real click.)
  if (!fullContentFetched && pass1.readMoreLinks.length > 0) {
    for (const link of pass1.readMoreLinks) {
      try {
        log.info('Following read-more: %s', link);
        const html2 = await fetchHTML(link, articleUrl);
        const pass2 = parseArticlePage(html2, link, rule);

        if (pass2.paragraphs.length > paragraphs.length) {
          paragraphs = pass2.paragraphs;
        }
        // Merge images
        for (const img of pass2.images) {
          if (!images.includes(img)) images.push(img);
        }
        premiumFlag = pass2.premium ?? premiumFlag;

        if (!isTruncated(pass2.paragraphs, html2)) {
          fullContentFetched = true;
          break;
        }
      } catch (e) {
        log.warn('read-more fetch failed: %s — %s', link, e);
      }
    }
  }

  // === PASS 3: If STILL not enough, try paginated next pages ===
  // (for sites that split long articles across pages)
  if (!fullContentFetched && paragraphs.length > 0) {
    // Try page 2 automatically for known multi-page articles
    const page2Candidates = [
      articleUrl.replace(/(\?|$)/, '?page=2'),
      articleUrl.endsWith('/') ? articleUrl + '2/' : articleUrl + '/2/',
    ];
    for (const candidate of page2Candidates) {
      if (candidate === articleUrl) continue;
      try {
        const html3 = await fetchHTML(candidate, articleUrl);
        if (html3.length > 2000) {
          const pass3 = parseArticlePage(html3, candidate, rule);
          if (pass3.paragraphs.length > 2) {
            paragraphs = [...paragraphs, ...pass3.paragraphs];
            for (const img of pass3.images) {
              if (!images.includes(img)) images.push(img);
            }
            fullContentFetched = true;
          }
        }
        break;
      } catch { /* not found, skip */ }
    }
  }

  const hero = meta.heroImage ? absoluteUrl(meta.heroImage, origin) : (images[0] || '');
  const allImages = hero ? [hero, ...images.filter(i => i !== hero)] : images;
  const source = rule?.name ?? new URL(articleUrl).hostname.replace(/^www\./, '');

  return {
    title: meta.title || '',
    author: meta.author,
    publishedAt: meta.publishedAt,
    heroImage: hero,
    heroImagePosition,
    images: allImages.slice(0, 10),
    paragraphs: paragraphs.slice(0, 100),
    source,
    sourceUrl: articleUrl,
    favicon: meta.favicon,
    readingTime: estimateReadingTime(paragraphs),
    fullContentFetched,
    // Only report premium if the READ MORE follow-through (PASS 2) genuinely
    // failed to surface more content — if it did, fullContentFetched is true
    // and we don't want to mislabel a successfully-expanded article.
    premium: premiumFlag && !fullContentFetched,
    isLiveBlog,
  };
}

// ─── Route ────────────────────────────────────────────────────────────────────
// ─── Resolve Google News redirect URLs to real article URLs ──────────────────
/**
 * All Google-owned / Google-infrastructure domains that must never be
 * treated as a "resolved" publisher article URL.
 */
const GOOGLE_DOMAINS = /(?:^|\.)(?:google|googleapis|googleusercontent|gstatic|ggpht|googlevideo|googletagmanager|googlesyndication|doubleclick|ampproject|g\.co|goo\.gl)(?:\.|$)/i;

/**
 * Return true if a candidate URL looks like a real publisher article URL —
 * not an image, asset, Google CDN URL, or other non-article resource.
 */
// Non-news domains that may appear in Google News HTML but are never article URLs
const NON_ARTICLE_DOMAINS = /(?:^|\.)(?:w3\.org|schema\.org|iana\.org|ietf\.org|whatwg\.org|xml\.org|xmlns\.com|purl\.org|dublincore\.org)(?:\.|$)/i;

function isRealArticleUrl(candidate: string): boolean {
  try {
    const u = new URL(candidate);
    // Must be http(s)
    if (!['http:', 'https:'].includes(u.protocol)) return false;
    // Must not be any Google-owned domain
    if (GOOGLE_DOMAINS.test(u.hostname)) return false;
    // Must not be a spec/standards/namespace domain (e.g. w3.org, schema.org)
    if (NON_ARTICLE_DOMAINS.test(u.hostname)) return false;
    // Must not be a static asset
    if (/\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|woff|woff2|ttf|eot|pdf|zip|mp4|mp3)(\?|$)/i.test(u.pathname)) return false;
    // Must not look like a CDN image path
    if (/\/(images?|assets?|static|media|upload|thumb|photo|img)\//i.test(u.pathname)) return false;
    // Must have a meaningful path (not just a domain root or 1-char path)
    if (u.pathname.length < 4) return false;
    // Must be at least 20 chars total (avoids fragment-only URLs etc.)
    if (candidate.length < 20) return false;
    return true;
  } catch {
    return false;
  }
}

// ── BusinessLine Case 1: Google News interstitial pages ──────────────────────
// Some BusinessLine (and other publisher) links reached via Google News land
// on a Google AMP-Viewer shell instead of the real article — a static page
// whose only content is "This article is hosted on <Publisher>" and an
// "OPEN ON <PUBLISHER>" link. Two ways this shell shows up, handled below:
//   (a) The URL itself is a Google AMP-Viewer URL of the form
//       https://google.com/amp/s/<real-domain>/<real-path> — the real
//       article URL is embedded directly in the path, no network call needed.
//   (b) The fetched HTML *content* matches the interstitial shell even though
//       the URL didn't look Google-owned (e.g. after a redirect chain) — in
//       that case we parse the shell's own "OPEN ON X" link out of the HTML.
// Both helpers are generic (not BusinessLine-specific) so this protects any
// source that comes in via a Google News link, per the requirement that this
// fix shouldn't only apply to BusinessLine.

// Shared depth guard between extractHostedOnInterstitialLink and
// resolveGoogleNewsUrl, which can call each other (the "OPEN ON X" link on
// a hosted-page shell is sometimes itself another Google News URL that
// needs a further round of resolution). Without this, a pathological chain
// of shells could recurse forever.
const MAX_GOOGLE_REDIRECT_DEPTH = 4;

/** Unwrap a Google AMP-Viewer URL (google.com/amp/s/<real-url>) to the real publisher URL. */
function unwrapAmpViewerUrl(url: string): string | null {
  try {
    const u = new URL(url);
    if (!GOOGLE_DOMAINS.test(u.hostname)) return null;
    const m = u.pathname.match(/^\/amp\/s\/(.+)$/i);
    if (!m) return null;
    const real = 'https://' + decodeURIComponent(m[1]);
    return isRealArticleUrl(real) ? real : null;
  } catch {
    return null;
  }
}

/**
 * Detect a Google "This article is hosted on X" interstitial shell by its
 * content and extract the real publisher URL from its "OPEN ON X" link.
 *
 * IMPORTANT: the "OPEN ON X" href is frequently *another* Google News
 * redirect URL, not the final publisher URL — Google's shell just proxies
 * the click through another hop. Returning that href as-is (the old bug)
 * sends the caller right back to the same hosted shell. So instead of
 * returning the raw href, this function keeps resolving — recursing into
 * resolveGoogleNewsUrl() — until it lands on an actual publisher URL (or
 * gives up at MAX_GOOGLE_REDIRECT_DEPTH).
 *
 * Returns null if the HTML doesn't look like this shell at all, or if no
 * publisher URL could be reached.
 */
async function extractHostedOnInterstitialLink(
  html: string,
  pageUrl: string,
  depth = 0
): Promise<string | null> {
  if (!/this\s+article\s+is\s+hosted\s+on/i.test(html)) return null;
  log.info('Hosted page detected (Google interstitial shell) at %s', pageUrl);

  if (depth >= MAX_GOOGLE_REDIRECT_DEPTH) {
    log.warn('Hosted-page redirect depth exceeded (%d) for %s — giving up', depth, pageUrl);
    return null;
  }

  let href = '';
  try {
    const root = parse(html, { lowerCaseTagName: false, comment: false });
    const anchors = root.querySelectorAll('a');
    // Prefer the anchor whose visible text is literally "OPEN ON <publisher>"
    for (const a of anchors) {
      const text = (a.text || '').trim();
      const h = a.getAttribute('href') || '';
      if (/^open\s+on\b/i.test(text) && h) { href = h; break; }
    }
    // Fall back to the first anchor with any href on the shell page
    if (!href) {
      for (const a of anchors) {
        const h = a.getAttribute('href') || '';
        if (h) { href = h; break; }
      }
    }
  } catch {
    // ignore parse errors — treat as unresolvable
  }

  if (!href) return null;

  const absHref = absoluteUrl(href, pageUrl);
  log.info('OPEN ON href: %s', absHref);

  // Case A: the href is an AMP-Viewer URL — unwrap it directly, no fetch needed.
  let candidate = unwrapAmpViewerUrl(absHref) ?? absHref;

  // Case B: the href is itself another Google News URL — this is the bug
  // fix. Keep resolving instead of returning this Google URL as "done".
  if (candidate.includes('news.google.com') || GOOGLE_DOMAINS.test(safeHostname(candidate))) {
    log.info('Resolving Google redirect...');
    candidate = await resolveGoogleNewsUrl(candidate, depth + 1);
  }

  // Final guard: never hand back a Google-owned URL (news.google.com,
  // googleusercontent.com, gstatic.com, ampproject.org, etc.) as "resolved".
  if (!isRealArticleUrl(candidate) || GOOGLE_DOMAINS.test(safeHostname(candidate))) {
    log.warn('Could not resolve past Google hosted shell for %s', pageUrl);
    return null;
  }

  log.info('Resolved publisher URL: %s', candidate);
  return candidate;
}

/** Hostname of a URL, or '' if unparseable — avoids try/catch clutter at call sites. */
function safeHostname(url: string): string {
  try { return new URL(url).hostname; } catch { return ''; }
}

/**
 * Resolve a news.google.com/rss/articles/... URL to the real publisher article URL.
 *
 * Strategies tried in order:
 *  1. HEAD request with redirect:follow — Google sometimes issues a 301/302
 *  2. data-n-au="URL" attribute in the rendered HTML
 *  3. jsdata / AF_initDataCallback / WIZ_global_data blobs in <script> tags
 *  4. Decoded base64 token embedded in the Google News URL itself
 *  5. og:url / canonical meta tags
 *  6. All quoted https:// strings inside <script> blocks
 *  7. All href attributes on <a> tags pointing off google.com
 *  8. Fall back to original URL
 */
const GNEWS_HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
  'Accept-Language': 'en-IN,en-US;q=0.9,en;q=0.8',
  'Accept-Encoding': 'gzip, deflate, br',
  'Cache-Control': 'no-cache',
  'Referer': 'https://news.google.com/',
  'Sec-Fetch-Dest': 'document',
  'Sec-Fetch-Mode': 'navigate',
  'Sec-Fetch-Site': 'none',
  'Upgrade-Insecure-Requests': '1',
};

// Decode a URL string and clean escape sequences
function decodeGoogleUrl(raw: string): string {
  return raw
    .replace(/\\u003d/gi, '=')
    .replace(/\\u0026/gi, '&')
    .replace(/\\u003a/gi, ':')
    .replace(/\\u002f/gi, '/')
    .replace(/\\x2f/gi, '/')
    .replace(/\\\//g, '/')
    .replace(/&amp;/g, '&');
}

/**
 * BusinessLine Case 1 (real fix): Google's "hosted on X" interim page isn't
 * a plain HTTP redirect — the final navigation is completed client-side by
 * JavaScript, which signs a request with a per-article token (`data-n-a-id`
 * / `data-n-a-ts` / `data-n-a-sg` embedded in that same interim page) and
 * exchanges it via Google's internal `batchexecute` RPC endpoint for the
 * real publisher URL. That's why a manual browser click resolves correctly
 * but re-fetching the same interstitial URL server-side just returns the
 * same shell again (no redirect ever happens without running that JS).
 *
 * This reproduces that token exchange with a plain HTTP POST — no browser
 * needed. It's a best-effort implementation of Google's own (undocumented,
 * reverse-engineered) protocol: if Google changes the RPC contract this can
 * stop working, so it's tried first but every other existing strategy in
 * resolveGoogleNewsUrl() still runs as a fallback if it fails.
 */
async function decodeGoogleNewsToken(url: string): Promise<string | null> {
  const idMatchFromUrl = url.match(/\/articles\/([^?/]+)/);
  if (!idMatchFromUrl) return null;

  try {
    const res = await fetch(url, {
      method: 'GET',
      redirect: 'follow',
      headers: GNEWS_HEADERS,
      signal: AbortSignal.timeout(10000),
    });
    const html = await res.text();

    const sg = html.match(/data-n-a-sg="([^"]+)"/)?.[1];
    const ts = html.match(/data-n-a-ts="([^"]+)"/)?.[1];
    const id = html.match(/data-n-a-id="([^"]+)"/)?.[1] || idMatchFromUrl[1];
    if (!sg || !ts) {
      log.warn('No signed token (data-n-a-sg/ts) found on interim page for id=%s (sg=%s, ts=%s)', id, !!sg, !!ts);
      return null;
    }

    log.info('Decoding Google News signed token (id=%s)…', id);

    const innerPayload = JSON.stringify([
      'garturlreq',
      [['X', 'X', ['X', 'X'], null, null, 1, 1, 'US:en', null, 1, null, null, null, null, null, 0, 1],
        'X', 'X', 1, [1, 2, 3, 4, 8], 1, 1, null, 0, 0, null, 0],
      id, Number(ts), sg,
    ]);
    const body = 'f.req=' + encodeURIComponent(JSON.stringify([[['Fbv4je', innerPayload, null, 'generic']]]));

    const beRes = await fetch(
      `https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je&source-path=/rss/articles/${id}&hl=en-US`,
      {
        method: 'POST',
        headers: {
          'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'User-Agent': GNEWS_HEADERS['User-Agent'],
        },
        body,
        signal: AbortSignal.timeout(10000),
      }
    );

    if (!beRes.ok) {
      log.warn('batchexecute HTTP %d for id=%s', beRes.status, id);
      return null;
    }

    const beText = (await beRes.text()).replace(/^\)\]\}'/, '');
    log.info('batchexecute response (id=%s, %d chars): %s', id, beText.length, beText.slice(0, 300));

    // Response is newline-delimited, length-prefixed JSON chunks with the
    // real URL embedded inside a *double*-JSON-encoded string — e.g.
    // ...,\"garturlres\",\"https://real-site.com/...\",1,... — so the URL
    // sits between ESCAPED quotes (\"), not plain ones. The previous regex
    // required a literal closing `"` right after the URL and silently
    // failed the instant it hit that escaping backslash instead, even
    // though the URL was plainly present in the response. This version
    // just captures a run of URL-safe characters bounded by whitespace,
    // a quote, or a backslash on either side — works whether the quotes
    // around it are escaped or not.
    const allUrlMatches = [...beText.matchAll(/(https?:\/\/[^\s"'\\]{10,})/g)].map(m => decodeGoogleUrl(m[1]));
    for (const candidate of allUrlMatches) {
      if (isRealArticleUrl(candidate)) return candidate;
    }
    if (allUrlMatches.length > 0) {
      log.warn('batchexecute returned %d URL(s) but none passed isRealArticleUrl: %s', allUrlMatches.length, allUrlMatches.slice(0, 5).join(' | '));
    } else {
      log.warn('batchexecute response contained no quoted https:// URLs at all for id=%s', id);
    }
    return null;
  } catch (e) {
    log.warn('Google News token decode failed for %s — %s', url, e);
    return null;
  }
}

async function resolveGoogleNewsUrl(url: string, depth = 0): Promise<string> {
  // BusinessLine Case 1: Google AMP-Viewer URLs (google.com/amp/s/...) carry
  // the real publisher URL directly in the path — unwrap them with no
  // network round-trip before falling through to the news.google.com logic.
  const ampUnwrapped = unwrapAmpViewerUrl(url);
  if (ampUnwrapped) return ampUnwrapped;

  if (!url.includes('news.google.com')) return url;

  log.info('Google News URL detected: %s', url);

  if (depth >= MAX_GOOGLE_REDIRECT_DEPTH) {
    log.warn('Google News redirect depth exceeded (%d) for %s — giving up', depth, url);
    return url;
  }

  // ── Strategy -1: signed token exchange (the real fix — see decodeGoogleNewsToken) ─
  const tokenResolved = await decodeGoogleNewsToken(url);
  if (tokenResolved) {
    log.info('Resolved publisher URL via signed token: %s', tokenResolved);
    return tokenResolved;
  }

  // ── Strategy 0: HEAD redirect (fast path) ──────────────────────────────
  try {
    const headRes = await fetch(url, {
      method: 'HEAD',
      redirect: 'follow',
      headers: GNEWS_HEADERS,
      signal: AbortSignal.timeout(6000),
    });
    if (headRes.url && isRealArticleUrl(headRes.url)) return headRes.url;
  } catch { /* continue */ }

  // Try both the /articles/ page and original /rss/articles/ URL
  const articlePageUrl = url.replace('/rss/articles/', '/articles/');
  const urlsToTry = articlePageUrl !== url ? [articlePageUrl, url] : [url];

  for (const tryUrl of urlsToTry) {
    try {
      const res = await fetch(tryUrl, {
        method: 'GET',
        redirect: 'follow',
        headers: GNEWS_HEADERS,
        signal: AbortSignal.timeout(14000),
      });

      // HTTP redirect landed off Google
      if (res.url && isRealArticleUrl(res.url)) return res.url;

      const html = await res.text();

      // ── Strategy 1: data-n-au attribute ──────────────────────────────────
      const dnauMatch = html.match(/data-n-au="([^"]+)"/);
      if (dnauMatch?.[1]) {
        const candidate = decodeGoogleUrl(dnauMatch[1]);
        if (isRealArticleUrl(candidate)) return candidate;
      }

      // ── Strategy 2: JS data blobs (AF_initDataCallback, WIZ_global_data) ─
      // Collect ALL https:// URLs found in scripts, score them, then pick the best.
      // Previous version returned the very first URL — which was almost always a
      // Google CDN / analytics URL, not the publisher article.
      const scriptBlocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/gi)];
      const scriptCandidates: string[] = [];
      for (const [, scriptContent] of scriptBlocks) {
        for (const [, rawUrl] of [...scriptContent.matchAll(/"(https?:\/\/[^"\\]{20,})"/g)]) {
          const candidate = decodeGoogleUrl(rawUrl);
          if (isRealArticleUrl(candidate)) scriptCandidates.push(candidate);
        }
        for (const [, rawUrl] of [...scriptContent.matchAll(/'(https?:\/\/[^'\\]{20,})'/g)]) {
          const candidate = decodeGoogleUrl(rawUrl);
          if (isRealArticleUrl(candidate)) scriptCandidates.push(candidate);
        }
      }
      // Score each candidate: prefer URLs with longer, news-like paths (slugs, digits, dashes)
      // and penalise known CDN / asset / API domains.
      const CDN_PENALTY = /(?:^|\.)(?:cdn|static|assets?|fonts?|images?|media|upload|s3|cloudfront|akamai|fastly|gstatic|googleapis|ytimg|fbcdn|twimg|pixel|beacon|tracking|analytics|stats|adservice|pagead|adsystem|urchin|chartbeat|parsely|mparticle|segment\.io|amplitude|mixpanel|hotjar|intercom|zendesk|hubspot|marketo)(?:\.|$)/i;
      function scoreCandidate(u: string): number {
        try {
          const pu = new URL(u);
          if (CDN_PENALTY.test(pu.hostname)) return -1;
          const path = pu.pathname;
          // Prefer paths that look like article slugs: contain dashes or digits and are long
          let score = path.length;
          if (/[\d]{4,}/.test(path)) score += 20;   // year or ID in path
          if (/-/.test(path)) score += 10;            // slug-like
          if (/\.(html?|aspx?|php|cms)$/i.test(path)) score += 5; // article extension
          return score;
        } catch { return -1; }
      }
      const bestScript = scriptCandidates
        .map(u => ({ u, score: scoreCandidate(u) }))
        .filter(x => x.score > 0)
        .sort((a, b) => b.score - a.score)[0]?.u;
      if (bestScript) return bestScript;

      // ── Strategy 3: Base64 token decode ──────────────────────────────────
      // Google News /articles/<base64token> sometimes encodes the real URL
      const tokenMatch = tryUrl.match(/\/articles\/([A-Za-z0-9_-]{20,})/);
      if (tokenMatch?.[1]) {
        try {
          // Pad base64url and decode
          const b64 = tokenMatch[1].replace(/-/g, '+').replace(/_/g, '/');
          const padded = b64 + '=='.slice(0, (4 - b64.length % 4) % 4);
          const decoded = Buffer.from(padded, 'base64').toString('utf-8');
          // The URL is usually preceded by some binary prefix; extract http(s)://
          const extractedUrl = decoded.match(/(https?:\/\/[^\s\x00-\x1f"'<>]{20,})/)?.[1];
          if (extractedUrl && isRealArticleUrl(extractedUrl)) return extractedUrl;
        } catch { /* not base64-decodeable */ }
      }

      // ── Strategy 4: og:url / canonical ───────────────────────────────────
      const ogUrlM = html.match(/<meta[^>]+property=["']og:url["'][^>]+content=["']([^"']+)["']/i)
                  ?? html.match(/<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']/i);
      if (ogUrlM?.[1] && isRealArticleUrl(ogUrlM[1])) return ogUrlM[1];

      // ── Strategy 5: <a href> links pointing off google.com ───────────────
      // Score candidates same as script URLs — avoid sharing widgets / CDN links.
      // Also unwrap AMP-Viewer links (google.com/amp/s/...) found among the
      // anchors, since a resolved-looking-Google href may still embed the
      // real BusinessLine (or other publisher) URL in its path.
      const anchorCandidates: string[] = [];
      for (const [, href] of [...html.matchAll(/<a[^>]+href=["'](https?:\/\/[^"']+)["']/gi)]) {
        const decoded = decodeGoogleUrl(href);
        const candidate = unwrapAmpViewerUrl(decoded) ?? decoded;
        if (isRealArticleUrl(candidate)) anchorCandidates.push(candidate);
      }
      const bestAnchor = anchorCandidates
        .map(u => ({ u, score: scoreCandidate(u) }))
        .filter(x => x.score > 0)
        .sort((a, b) => b.score - a.score)[0]?.u;
      if (bestAnchor) return bestAnchor;

      // ── Strategy 6: "This article is hosted on X / OPEN ON X" shell ──────
      // BusinessLine Case 1: covers the AMP-Viewer interstitial when none of
      // the above strategies picked it up (e.g. its "OPEN ON X" link isn't
      // the highest-scoring anchor by the generic slug heuristic above).
      const interstitialLink = await extractHostedOnInterstitialLink(html, tryUrl, depth + 1);
      if (interstitialLink) return interstitialLink;

    } catch {
      // Network error — try next URL variant
    }
  }

  // Could not resolve — return original; UI shows "open externally" fallback
  return url;
}

export async function GET(req: NextRequest) {
  const rawUrl    = req.nextUrl.searchParams.get('url');
  const sourceUrl = req.nextUrl.searchParams.get('sourceUrl') || '';  // real publisher URL hint

  if (!rawUrl) return NextResponse.json({ error: 'Missing ?url=' }, { status: 400 });

  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
    if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('bad proto');
  } catch {
    return NextResponse.json({ error: 'Invalid URL' }, { status: 400 });
  }

  try {
    // Resolve Google News proxy URLs to actual publisher article URLs
    let resolvedUrl = await resolveGoogleNewsUrl(parsed.href);

    // Safety guard: if resolution returned a non-article URL (e.g. w3.org namespace),
    // fall back to the original URL so scrapeArticle can show the "open externally" fallback
    if (!isRealArticleUrl(resolvedUrl) && resolvedUrl !== parsed.href) {
      resolvedUrl = parsed.href;
    }

    // If the primary URL is still a Google News proxy (resolution failed server-side),
    // fall back to the real publisher URL supplied by the feed scraper via sourceUrl param.
    const isStillGoogleProxy = GOOGLE_DOMAINS.test(new URL(resolvedUrl).hostname);
    if (isStillGoogleProxy && sourceUrl && isRealArticleUrl(sourceUrl)) {
      log.warn('Google resolution failed — falling back to sourceUrl: %s', sourceUrl);
      resolvedUrl = sourceUrl;
    }

    let content;

try {
    content = await scrapeArticle(resolvedUrl);
} catch (err) {

    log.warn(
        'Article fallback failed: %s — %s',
        resolvedUrl,
        err
    );

    const source =
        new URL(resolvedUrl)
            .hostname
            .replace(/^www\./, '');

    content = {
        title: '',
        author: '',
        publishedAt: '',
        heroImage: '',
        heroImagePosition: 'afterLede',
        images: [],
        paragraphs: [],
        source,
        sourceUrl: resolvedUrl,
        favicon: '',
        readingTime: 1,
        fullContentFetched: false,
    };
}
    return NextResponse.json(content, {
      headers: { 'Cache-Control': 's-maxage=1800, stale-while-revalidate=300' },
    });
  } catch (err) {
    log.error('Article handler error: %s', err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : 'Scrape failed' },
      { status: 502 }
    );
  }
}
