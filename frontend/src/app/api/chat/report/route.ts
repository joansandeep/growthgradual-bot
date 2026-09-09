/**
 * Thin proxy → Python FastAPI backend /api/chat/report
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 180; // 3 min — report generation is slow (LLM + web search)

const log = createLogger('api/chat/report');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/chat/report');
  const body = await req.arrayBuffer();

  let upstream: Response;
  const fetchReport = () => fetch(`${BACKEND}/api/chat/report`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
    // Keep a hard upper bound below the platform route limit.
    signal: AbortSignal.timeout(170_000),
  });
  try {
    upstream = await fetchReport();
    // A cold-start/transient upstream 502/503 is worth one immediate retry.
    // Do not retry 504/timeouts because those usually represent a genuinely
    // long report job and a second full attempt would make the UX worse.
    if (upstream.status === 502 || upstream.status === 503) {
      await new Promise(r => setTimeout(r, 800));
      upstream = await fetchReport();
    }
  } catch (err) {
    const timedOut = err instanceof Error && (err.name === 'TimeoutError' || err.name === 'AbortError');
    log.error('Backend unreachable or timed out: %s', err);
    done(timedOut ? 504 : 502, timedOut ? 'backend timeout' : 'backend unreachable');
    return NextResponse.json(
      { error: timedOut
        ? 'Report generation took too long and timed out. Please try again — a more specific question can help.'
        : 'Backend unavailable' },
      { status: timedOut ? 504 : 502 },
    );
  }

  // Guard: upstream may return non-JSON (e.g. "Too Many Requests" plain text on 429)
  const contentType = upstream.headers.get('Content-Type') ?? '';
  if (!contentType.includes('application/json')) {
    const text = await upstream.text();
    log.warn('Non-JSON response from backend (%d): %s', upstream.status, text.slice(0, 120));
    done(upstream.status, 'non-json upstream');
    return NextResponse.json(
      { error: upstream.status === 503
        ? 'The report service is temporarily overloaded. Please retry in a moment.'
        : upstream.status === 504
          ? 'Report generation timed out. Please retry in a moment.'
          : `Backend returned ${upstream.status}: ${text.slice(0, 200)}` },
      { status: upstream.status },
    );
  }

  let data: unknown;
  try {
    data = await upstream.json();
  } catch (err) {
    const raw = await upstream.text().catch(() => '');
    log.error('Failed to parse backend JSON (%d): %s — %s', upstream.status, err, raw.slice(0, 120));
    done(upstream.status, 'json parse error');
    return NextResponse.json({ error: 'Invalid response from backend' }, { status: 502 });
  }

  const title = (typeof data === 'object' && data && 'title' in data)
    ? String((data as Record<string, unknown>).title ?? '').slice(0, 40)
    : '';
  done(upstream.status, `title="${title}"`);
  return NextResponse.json(data, { status: upstream.status });
}
