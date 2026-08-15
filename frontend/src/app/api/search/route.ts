/**
 * Thin proxy → Python FastAPI backend /api/datasearch
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 180; // query expansion + up to 5 search angles + batched extraction

const log = createLogger('api/search');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/search');
  const body = await req.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/api/datasearch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: AbortSignal.timeout(170_000),
    });
  } catch (err) {
    const timedOut = err instanceof Error && (err.name === 'TimeoutError' || err.name === 'AbortError');
    log.error('Backend unreachable or timed out: %s', err);
    done(timedOut ? 504 : 502, timedOut ? 'backend timeout' : 'backend unreachable');
    return NextResponse.json(
      { error: timedOut
        ? 'Data search took too long and timed out. Please try again — a more specific query can help.'
        : 'Backend unavailable' },
      { status: timedOut ? 504 : 502 },
    );
  }

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

  const n = (typeof data === 'object' && data && 'dataPoints' in data)
    ? (data as Record<string, unknown[]>).dataPoints?.length ?? 0
    : 0;
  done(upstream.status, `dataPoints=${n}`);
  return NextResponse.json(data, { status: upstream.status });
}
