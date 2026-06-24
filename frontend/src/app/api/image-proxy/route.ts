/**
 * GET /api/image-proxy?url=<encoded-image-url>
 *
 * Server-side image proxy. Tavily's image results from Indian financial news
 * sites (moneycontrol.com, economictimes.com, etc.) block cross-origin
 * requests with hotlink protection — a bare <img src="..."> in the browser
 * gets a 403. Fetching through this Next.js route uses a browser-like
 * User-Agent and runs server-side (same-origin from the browser's perspective),
 * so hotlink checks pass and the image loads.
 *
 * Security: only proxies http(s) URLs; rejects obvious non-image content-types.
 */
import { NextRequest, NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// Domains we're willing to proxy — keeps this from being an open proxy
const ALLOWED_DOMAINS = [
  'economictimes.indiatimes.com',
  'etimg.com',
  'img.etimg.com',
  'moneycontrol.com',
  'images.moneycontrol.com',
  'static-news.moneycontrol.com',
  'bsmedia.business-standard.com',
  'business-standard.com',
  'financialexpress.com',
  'images.financialexpress.com',
  'livemint.com',
  'images.livemint.com',
  'thehindu.com',
  'bl-i.thgim.com',
  'ndtvprofit.com',
  'media.ndtvprofit.com',
  'zeebiz.com',
  'crisil.com',
  'screener.in',
  'tickertape.in',
  'trendlyne.com',
  'tavily.com',
  'upload.wikimedia.org',
  'reuters.com',
  'bloombergquint.com',
];

function isAllowed(url: string): boolean {
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol)) return false;
    const host = parsed.hostname.replace(/^www\./, '');
    return ALLOWED_DOMAINS.some(d => host === d || host.endsWith('.' + d));
  } catch {
    return false;
  }
}

export async function GET(req: NextRequest) {
  const rawUrl = req.nextUrl.searchParams.get('url') ?? '';
  if (!rawUrl) {
    return new NextResponse('Missing url param', { status: 400 });
  }

  let targetUrl: string;
  try {
    targetUrl = decodeURIComponent(rawUrl);
  } catch {
    return new NextResponse('Invalid url encoding', { status: 400 });
  }

  if (!isAllowed(targetUrl)) {
    return new NextResponse('Domain not allowed', { status: 403 });
  }

  try {
    const upstream = await fetch(targetUrl, {
      headers: {
        'User-Agent':
          'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
          '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': new URL(targetUrl).origin + '/',
      },
      signal: AbortSignal.timeout(8_000),
    });

    if (!upstream.ok) {
      return new NextResponse(`Upstream ${upstream.status}`, { status: upstream.status });
    }

    const contentType = upstream.headers.get('content-type') ?? '';
    if (!contentType.startsWith('image/')) {
      return new NextResponse('Not an image', { status: 415 });
    }

    const bytes = await upstream.arrayBuffer();
    return new NextResponse(bytes, {
      status: 200,
      headers: {
        'Content-Type': contentType,
        'Cache-Control': 'public, max-age=86400, stale-while-revalidate=604800',
        'Content-Length': String(bytes.byteLength),
      },
    });
  } catch (err) {
    console.error('[image-proxy] fetch failed:', err);
    return new NextResponse('Proxy fetch failed', { status: 502 });
  }
}
