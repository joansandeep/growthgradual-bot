/**
 * Thin proxy → Python FastAPI backend /api/chat/report/pdf
 * Returns raw PDF bytes (application/pdf)
 * Injects logoB64 from logos.ts so the backend can render the logo even on servers
 * where the frontend/public directory isn't accessible.
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';
import { LOGO_B64 } from './logos';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 300;

const log = createLogger('api/chat/report/pdf');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/chat/report/pdf');

  // Parse the incoming body, inject logoB64, then re-serialise
  let bodyObj: Record<string, unknown>;
  try {
    bodyObj = await req.json();
  } catch {
    done(400, 'invalid json');
    return NextResponse.json({ error: 'Invalid request body' }, { status: 400 });
  }

  // Attach the logo so the Python PDF generator can render it regardless of filesystem layout
  bodyObj.logoB64 = LOGO_B64;

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/api/chat/report/pdf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(bodyObj),
      signal: AbortSignal.timeout(285_000),
    });
  } catch (err) {
    log.error('Backend unreachable or timed out: %s', err);
    const timedOut = err instanceof Error && (err.name === 'TimeoutError' || err.name === 'AbortError');
    done(timedOut ? 504 : 502, timedOut ? 'backend timeout' : 'backend unreachable');
    return NextResponse.json({ error: timedOut
      ? 'PDF generation took too long. The report is ready; please retry the PDF export in a moment.'
      : 'The report service is temporarily unavailable. Please retry the PDF export.'
    }, { status: timedOut ? 504 : 502 });
  }

  if (!upstream.ok) {
    const raw = await upstream.text();
    const contentType = (upstream.headers.get('content-type') ?? '').toLowerCase();
    // Never surface HTML/framework error pages or giant CSS/font payloads to the
    // report UI. Only accept a small JSON error envelope from the backend.
    let message = `PDF generation failed (HTTP ${upstream.status}).`;
    if (contentType.includes('application/json')) {
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object' && typeof (parsed as any).error === 'string' && (parsed as any).error.trim()) {
          message = String((parsed as any).error).slice(0, 500);
        }
      } catch { /* keep generic message */ }
    }
    log.error('PDF generation failed upstream: HTTP %d — %s', upstream.status, raw.slice(0, 160));
    done(upstream.status, 'upstream error');
    return NextResponse.json({ error: message }, { status: upstream.status });
  }

  const pdfBuffer = await upstream.arrayBuffer();
  const contentType = (upstream.headers.get('content-type') ?? '').toLowerCase();
  const header = new Uint8Array(pdfBuffer.slice(0, 5));
  const pdfMagic = header.length === 5 && header[0] === 0x25 && header[1] === 0x50 && header[2] === 0x44 && header[3] === 0x46 && header[4] === 0x2d;
  if (!contentType.includes('application/pdf') || !pdfMagic) {
    log.error('PDF upstream returned a non-PDF success response: content-type=%s bytes=%d', contentType, pdfBuffer.byteLength);
    done(502, 'invalid pdf response');
    return NextResponse.json({ error: 'The PDF service returned an invalid document. Please retry the PDF export.' }, { status: 502 });
  }
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
