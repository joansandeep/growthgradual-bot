/**
 * Thin proxy → Python FastAPI backend /api/chat/report
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

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
    });
  } catch (err) {
    log.error('Backend unreachable: %s', err);
    done(502, 'backend unreachable');
    return NextResponse.json({ error: 'Backend unavailable' }, { status: 502 });
  }

  const data = await upstream.json();
  done(upstream.status, `title="${(data?.title ?? '').slice(0, 40)}"`);
  return NextResponse.json(data, { status: upstream.status });
}
