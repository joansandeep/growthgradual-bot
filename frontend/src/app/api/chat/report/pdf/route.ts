/**
 * Thin proxy → Python FastAPI backend /api/chat/report/pdf
 * Returns raw PDF bytes (application/pdf)
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 60;

const log = createLogger('api/chat/report/pdf');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/chat/report/pdf');
  const body = await req.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/api/chat/report/pdf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: AbortSignal.timeout(55_000),
    });
  } catch (err) {
    log.error('Backend unreachable or timed out: %s', err);
    done(502, 'backend unreachable');
    return NextResponse.json({ error: 'Backend unavailable' }, { status: 502 });
  }

  if (!upstream.ok) {
    const err = await upstream.text();
    log.error('PDF generation failed upstream: HTTP %d — %s', upstream.status, err.slice(0, 120));
    done(upstream.status, 'upstream error');
    return NextResponse.json({ error: err }, { status: upstream.status });
  }

  const pdfBuffer = await upstream.arrayBuffer();
  const dateStr = new Date().toISOString().slice(0, 10);
  done(200, `${(pdfBuffer.byteLength / 1024).toFixed(1)} KB`);
  return new NextResponse(pdfBuffer, {
    headers: {
      'Content-Type': 'application/pdf',
      'Content-Disposition': `attachment; filename="growth-gradual-report-${dateStr}.pdf"`,
      'Cache-Control': 'no-store',
    },
  });
}
