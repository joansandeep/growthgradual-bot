import { NextResponse } from 'next/server';
import { promises as fs } from 'fs';
import path from 'path';
import { createLogger } from '@/lib/logger';

const log = createLogger('api/market');

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// ── Persistent disk cache (survives server restarts) ──────────────────────
const DISK_CACHE_PATH = path.join(process.cwd(), 'market_cache.json');
const MEM_TTL_MS  =  60_000; // 60s in-memory cache (serves hot repeat requests)
const DISK_TTL_MS = 300_000; // 5min disk cache (serves cold starts + API failures)
const STALE_OK_MS = 3_600_000; // 1hr — still serve stale data rather than nothing

let memCache: { data: MarketData; fetchedAt: number } | null = null;

// ── Types ─────────────────────────────────────────────────────────────────
export interface QuoteItem {
  symbol: string;
  price: string;
  change: string;
  up: boolean;
  raw: number;
}
export interface MarketData {
  stocks: QuoteItem[];
  stats: StatItem[];
  gainers: QuoteItem[];
  losers: QuoteItem[];
  marketOpen: boolean;
  fetchedAt: string;
  fromCache: boolean;
  cacheAge?: string;  // human-readable: "3m ago", "1h ago"
  stale?: boolean;
  error?: string;
}
export interface StatItem {
  label: string;
  value: string;
  sub: string;
}

// ── Formatters ────────────────────────────────────────────────────────────
function fmtAge(ms: number): string {
  const diff = Date.now() - ms;
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}
function fmtPrice(n: number, prefix = '₹', decimals = 2): string {
  if (!n) return '—';
  return prefix + n.toLocaleString('en-IN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}
function fmtPct(pct: number | null | undefined): string {
  if (pct == null || isNaN(pct)) return '—';
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

// ── Disk cache helpers ────────────────────────────────────────────────────
async function readDiskCache(): Promise<{ data: MarketData; fetchedAt: number } | null> {
  try {
    const raw = await fs.readFile(DISK_CACHE_PATH, 'utf8');
    const parsed = JSON.parse(raw);
    if (parsed?.data && parsed?.fetchedAt) return parsed;
  } catch { /* cache miss */ }
  return null;
}
async function writeDiskCache(data: MarketData, fetchedAt: number) {
  try {
    await fs.writeFile(DISK_CACHE_PATH, JSON.stringify({ data, fetchedAt }));
  } catch { /* ignore write errors */ }
}

// ── Yahoo Finance v8 chart API (more reliable than v7/quote) ─────────────
// Falls back to Stooq CSV if Yahoo returns 401/429
const YF_HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  'Accept': 'application/json',
  'Accept-Language': 'en-US,en;q=0.9',
  'Referer': 'https://finance.yahoo.com/',
};

interface QuoteResult {
  symbol: string;
  price: number;
  changePct: number;
  marketState?: string;
}

// Strategy A: Yahoo Finance crumb-based v7 quote (requires cookie+crumb fetch first)
async function yfCrumbQuotes(symbols: string[]): Promise<Map<string, QuoteResult>> {
  const result = new Map<string, QuoteResult>();
  try {
    // Step 1: get a crumb cookie
    const cookieRes = await fetch('https://finance.yahoo.com/', {
      headers: YF_HEADERS,
      signal: AbortSignal.timeout(8_000),
    });
    const setCookie = cookieRes.headers.get('set-cookie') ?? '';
    const cookieMatch = setCookie.match(/A1=([^;]+)/);
    const cookieVal = cookieMatch ? `A1=${cookieMatch[1]}` : '';

    // Step 2: fetch crumb
    const crumbRes = await fetch('https://query1.finance.yahoo.com/v1/test/getcrumb', {
      headers: { ...YF_HEADERS, ...(cookieVal ? { Cookie: cookieVal } : {}) },
      signal: AbortSignal.timeout(6_000),
    });
    if (!crumbRes.ok) throw new Error(`crumb HTTP ${crumbRes.status}`);
    const crumb = (await crumbRes.text()).trim();
    if (!crumb || crumb.length > 40) throw new Error('bad crumb');

    // Step 3: actual quote fetch with crumb
    const joined = symbols.join(',');
    const url = `https://query1.finance.yahoo.com/v7/finance/quote?symbols=${joined}&crumb=${encodeURIComponent(crumb)}&fields=regularMarketPrice,regularMarketChangePercent,marketState`;
    const res = await fetch(url, {
      headers: { ...YF_HEADERS, ...(cookieVal ? { Cookie: cookieVal } : {}) },
      signal: AbortSignal.timeout(12_000),
    });
    if (!res.ok) throw new Error(`YF quote HTTP ${res.status}`);
    const json = await res.json() as { quoteResponse?: { result?: Array<{
      symbol: string; regularMarketPrice?: number;
      regularMarketChangePercent?: number; marketState?: string;
    }> } };
    for (const q of json.quoteResponse?.result ?? []) {
      if (q.regularMarketPrice) {
        result.set(q.symbol, {
          symbol: q.symbol,
          price: q.regularMarketPrice,
          changePct: q.regularMarketChangePercent ?? 0,
          marketState: q.marketState,
        });
      }
    }
    log.info('YF crumb fetch: %d/%d symbols', result.size, symbols.length);
  } catch (e) {
    log.warn('YF crumb fetch failed: %s', e);
  }
  return result;
}

// Strategy B: Stooq CSV (free, no auth, Indian IPs work well)
async function stooqQuote(symbol: string): Promise<{ price: number; change: number } | null> {
  try {
    const res = await fetch(`https://stooq.com/q/l/?s=${symbol}&f=sd2t2ohlcv&h&e=csv`, {
      headers: { 'User-Agent': 'Mozilla/5.0' },
      signal: AbortSignal.timeout(8_000),
    });
    const text = await res.text();
    const lines = text.trim().split('\n');
    if (lines.length < 2) return null;
    const cols = lines[1].split(',');
    const close = parseFloat(cols[6]);
    const open  = parseFloat(cols[4]);
    if (!close || !open) return null;
    return { price: close, change: ((close - open) / open) * 100 };
  } catch { return null; }
}

// Strategy C: Yahoo Finance v8 chart endpoint (no auth required as of mid-2025)
async function yfChartQuote(symbol: string): Promise<QuoteResult | null> {
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=1d&range=1d`;
    const res = await fetch(url, {
      headers: YF_HEADERS,
      signal: AbortSignal.timeout(8_000),
    });
    if (!res.ok) return null;
    const json = await res.json() as {
      chart?: { result?: Array<{
        meta?: { regularMarketPrice?: number; chartPreviousClose?: number; marketState?: string };
      }> }
    };
    const meta = json.chart?.result?.[0]?.meta;
    if (!meta?.regularMarketPrice) return null;
    const prev  = meta.chartPreviousClose ?? meta.regularMarketPrice;
    const price = meta.regularMarketPrice;
    const changePct = prev ? ((price - prev) / prev) * 100 : 0;
    return { symbol, price, changePct, marketState: meta.marketState };
  } catch { return null; }
}

// ── Symbol lists ──────────────────────────────────────────────────────────
const INDEX_SYMBOLS: Array<{ stooq: string; yf: string; label: string }> = [
  { stooq: '^nsei',   yf: '^NSEI',    label: 'NIFTY 50'   },
  { stooq: '^bsesn',  yf: '^BSESN',   label: 'SENSEX'     },
  { stooq: '^nsebank',yf: '^NSEBANK',  label: 'NIFTY BANK' },
  { stooq: '^cnxit',  yf: '^CNXIT',    label: 'NIFTY IT'   },
  { stooq: '^cnxmid', yf: '^CNXMID',   label: 'NIFTY MID'  },
];
const STOCK_SYMBOLS: Array<{ stooq: string; yf: string; label: string }> = [
  { stooq: 'reliance.ns', yf: 'RELIANCE.NS',   label: 'RELIANCE'    },
  { stooq: 'tcs.ns',      yf: 'TCS.NS',         label: 'TCS'         },
  { stooq: 'hdfcbank.ns', yf: 'HDFCBANK.NS',    label: 'HDFC BANK'   },
  { stooq: 'infy.ns',     yf: 'INFY.NS',         label: 'INFOSYS'     },
  { stooq: 'icicibank.ns',yf: 'ICICIBANK.NS',    label: 'ICICI BANK'  },
  { stooq: 'wipro.ns',    yf: 'WIPRO.NS',        label: 'WIPRO'       },
  { stooq: 'bajfinance.ns',yf:'BAJFINANCE.NS',   label: 'BAJAJ FIN'   },
  { stooq: 'kotakbank.ns',yf: 'KOTAKBANK.NS',    label: 'KOTAK'       },
  { stooq: 'maruti.ns',   yf: 'MARUTI.NS',       label: 'MARUTI'      },
  { stooq: 'sunpharma.ns',yf: 'SUNPHARMA.NS',    label: 'SUN PHARMA'  },
  { stooq: 'tatamotors.ns',yf:'TATAMOTORS.NS',   label: 'TATA MOTORS' },
  { stooq: 'axisbank.ns', yf: 'AXISBANK.NS',     label: 'AXIS BANK'   },
];

// ── Build full market payload ─────────────────────────────────────────────
async function buildMarketData(): Promise<MarketData> {
  // Try crumb-based YF first (most data in one shot), then fall back per-symbol to Stooq
  const allYFSyms = [
    ...INDEX_SYMBOLS.map(s => s.yf),
    ...STOCK_SYMBOLS.map(s => s.yf),
    'USDINR=X',
  ];

  const [yfMap, goldResult, crudeResult] = await Promise.allSettled([
    yfCrumbQuotes(allYFSyms),
    stooqQuote('xauusd'),
    stooqQuote('cl.f'),
  ]);

  let yf = yfMap.status === 'fulfilled' ? yfMap.value : new Map<string, QuoteResult>();

  // If crumb approach got < 30% symbols, supplement with per-symbol chart endpoint
  if (yf.size < allYFSyms.length * 0.3) {
    log.info('YF crumb got few results — supplementing with v8 chart');
    const chartFetches = allYFSyms.map(async sym => {
      if (yf.has(sym)) return;
      const r = await yfChartQuote(sym);
      if (r) yf.set(sym, r);
    });
    await Promise.allSettled(chartFetches);
    log.info('After v8 supplement: %d/%d', yf.size, allYFSyms.length);
  }

  // If YF still failing, fall back to Stooq for indices+top stocks
  if (yf.size < 3) {
    log.warn('YF mostly failed — using Stooq fallback for key symbols');
    const stooqFetches = [...INDEX_SYMBOLS, ...STOCK_SYMBOLS.slice(0, 6)].map(async s => {
      if (yf.has(s.yf)) return;
      const r = await stooqQuote(s.stooq);
      if (r) yf.set(s.yf, { symbol: s.yf, price: r.price, changePct: r.change });
    });
    // Also USD/INR from Stooq
    stooqFetches.push((async () => {
      const r = await stooqQuote('usd/inr');
      if (r) yf.set('USDINR=X', { symbol: 'USDINR=X', price: r.price, changePct: r.change });
    })());
    await Promise.allSettled(stooqFetches);
    log.info('After Stooq fallback: %d symbols', yf.size);
  }

  const gold  = goldResult.status  === 'fulfilled' ? goldResult.value  : null;
  const crude = crudeResult.status === 'fulfilled' ? crudeResult.value : null;

  const usdInrQ     = yf.get('USDINR=X');
  const usdInrPrice = usdInrQ?.price ?? null;
  const usdInrChg   = usdInrQ?.changePct ?? null;
  const niftyQ      = yf.get('^NSEI');
  const marketOpen  = niftyQ?.marketState === 'REGULAR';

  // Index rows
  const stockItems: QuoteItem[] = INDEX_SYMBOLS.map(({ yf: sym, label }) => {
    const q = yf.get(sym);
    return {
      symbol: label, price: q?.price ? fmtPrice(q.price, '', 0) : '—',
      change: fmtPct(q?.changePct), up: (q?.changePct ?? 0) >= 0, raw: q?.price ?? 0,
    };
  });
  stockItems.push({
    symbol: 'USD/INR',
    price: usdInrPrice ? `₹${usdInrPrice.toFixed(2)}` : '—',
    change: fmtPct(usdInrChg), up: (usdInrChg ?? 0) >= 0, raw: usdInrPrice ?? 0,
  });
  STOCK_SYMBOLS.slice(0, 6).forEach(({ yf: sym, label }) => {
    const q = yf.get(sym);
    stockItems.push({
      symbol: label, price: q?.price ? fmtPrice(q.price, '₹', 0) : '—',
      change: fmtPct(q?.changePct), up: (q?.changePct ?? 0) >= 0, raw: q?.price ?? 0,
    });
  });

  const allStockItems: QuoteItem[] = STOCK_SYMBOLS.map(({ yf: sym, label }) => {
    const q = yf.get(sym);
    return {
      symbol: label, price: q?.price ? fmtPrice(q.price, '₹', 0) : '—',
      change: fmtPct(q?.changePct), up: (q?.changePct ?? 0) >= 0, raw: q?.price ?? 0,
    };
  }).filter(s => s.raw > 0);

  const sorted  = [...allStockItems].sort((a, b) =>
    parseFloat(b.change.replace(/[^-\d.]/g, '')) - parseFloat(a.change.replace(/[^-\d.]/g, ''))
  );
  const gainers = sorted.filter(s => s.up).slice(0, 4);
  const losers  = [...sorted].reverse().filter(s => !s.up).slice(0, 3);

  const goldInr = gold?.price && usdInrPrice
    ? (gold.price * usdInrPrice / 31.1035) * 10 : 0;

  const statItems: StatItem[] = [
    { label: 'USD/INR',         value: usdInrPrice ? `₹${usdInrPrice.toFixed(2)}` : '—', sub: usdInrChg != null ? fmtPct(usdInrChg) + ' today' : 'Unavailable' },
    { label: 'Gold (MCX ~10g)', value: goldInr ? `₹${Math.round(goldInr).toLocaleString('en-IN')}` : '—', sub: gold?.price ? `$${gold.price.toFixed(0)}/oz` : 'Unavailable' },
    { label: 'Crude Oil',       value: crude?.price ? `$${crude.price.toFixed(2)}` : '—', sub: crude ? fmtPct(crude.change) : 'Unavailable' },
    { label: 'NIFTY 50',        value: niftyQ?.price ? fmtPrice(niftyQ.price, '', 0) : '—', sub: niftyQ ? fmtPct(niftyQ.changePct) + ' today' : 'Unavailable' },
    { label: 'Repo Rate',        value: '6.00%', sub: 'RBI — Jun 2026' },
  ];

  return {
    stocks: stockItems, stats: statItems, gainers, losers,
    marketOpen, fetchedAt: new Date().toISOString(), fromCache: false,
  };
}

// ── GET /api/market ───────────────────────────────────────────────────────
export async function GET() {
  const t0 = performance.now();

  // 1. Hot memory cache (60s)
  if (memCache && Date.now() - memCache.fetchedAt < MEM_TTL_MS) {
    log.debug('← GET /api/market  200  memory cache hit (%s)', fmtAge(memCache.fetchedAt));
    return NextResponse.json({ ...memCache.data, fromCache: true, cacheAge: fmtAge(memCache.fetchedAt) });
  }

  // 2. Try fresh fetch
  log.info('→ GET /api/market  fetching live data');
  try {
    const data = await buildMarketData();
    const hasRealData = data.stocks.some(s => s.raw > 0);

    if (hasRealData) {
      memCache = { data, fetchedAt: Date.now() };
      await writeDiskCache(data, Date.now());
      log.info('← GET /api/market  200  live  %.0fms', performance.now() - t0);
      return NextResponse.json(data);
    }
    throw new Error('All data sources returned empty');
  } catch (err) {
    log.warn('Live fetch failed: %s', err);
  }

  // 3. Memory cache (stale but fresh enough)
  if (memCache && Date.now() - memCache.fetchedAt < STALE_OK_MS) {
    const age = fmtAge(memCache.fetchedAt);
    log.warn('← GET /api/market  200  stale memory cache (%s)', age);
    return NextResponse.json({ ...memCache.data, fromCache: true, stale: true, cacheAge: age });
  }

  // 4. Disk cache (survives restarts)
  const disk = await readDiskCache();
  if (disk && Date.now() - disk.fetchedAt < STALE_OK_MS) {
    const age = fmtAge(disk.fetchedAt);
    memCache = { data: disk.data, fetchedAt: disk.fetchedAt };
    log.warn('← GET /api/market  200  stale disk cache (%s)', age);
    return NextResponse.json({ ...disk.data, fromCache: true, stale: true, cacheAge: age });
  }

  log.error('← GET /api/market  502  all sources exhausted in %.0fms', performance.now() - t0);
  return NextResponse.json({ error: 'Market data unavailable' }, { status: 502 });
}
