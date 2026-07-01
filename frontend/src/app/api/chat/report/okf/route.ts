/**
 * Thin proxy → Python FastAPI backend /api/chat/report/okf
 * Returns raw OKF bundle bytes (application/zip) — a directory of markdown
 * concept files with YAML frontmatter, per Google Cloud's Open Knowledge
 * Format v0.1 spec, replacing the old PDF download for reports.
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 120;

const log = createLogger('api/chat/report/okf');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/chat/report/okf');

  let bodyObj: Record<string, unknown>;
  try {
    bodyObj = await req.json();
  } catch {
    done(400, 'invalid json');
    return NextResponse.json({ error: 'Invalid request body' }, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/api/chat/report/okf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(bodyObj),
      signal: AbortSignal.timeout(115_000),
    });
  } catch (err) {
    log.error('Backend unreachable or timed out: %s', err);
    done(502, 'backend unreachable');
    return NextResponse.json({ error: 'Backend unavailable' }, { status: 502 });
  }

  if (!upstream.ok) {
    const err = await upstream.text();
    log.error('OKF generation failed upstream: HTTP %d — %s', upstream.status, err.slice(0, 120));
    done(upstream.status, 'upstream error');
    return NextResponse.json({ error: err }, { status: upstream.status });
  }

  const zipBuffer = await upstream.arrayBuffer();
  const dateStr = new Date().toISOString().slice(0, 10);
  done(200, `${(zipBuffer.byteLength / 1024).toFixed(1)} KB`);
  return new NextResponse(zipBuffer, {
    headers: {
      'Content-Type': 'application/zip',
      'Content-Disposition': `attachment; filename="growth-gradual-report-okf-${dateStr}.zip"`,
      'Cache-Control': 'no-store',
    },
  });
}
