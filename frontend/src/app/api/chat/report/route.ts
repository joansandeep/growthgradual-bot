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
  try {
    upstream = await fetch(`${BACKEND}/api/chat/report`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      // Bound this explicitly to just under maxDuration so we always return
      // a clean JSON error in-band. Without this, a slow upstream (Gemini
      // retry cascade running past the platform's own gateway/function
      // timeout) gets its connection killed uncleanly — the fetch never
      // resolves through our try/catch, and the caller can end up treating
      // whatever partial/empty response comes back as a "successful" report.
      signal: AbortSignal.timeout(170_000),
    });
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
      { error: `Backend returned ${upstream.status}: ${text.slice(0, 200)}` },
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
