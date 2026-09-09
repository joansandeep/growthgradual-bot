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
export const maxDuration = 180;

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
  const fetchPdf = () => fetch(`${BACKEND}/api/chat/report/pdf`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(bodyObj),
    signal: AbortSignal.timeout(165_000),
  });
  try {
    upstream = await fetchPdf();
    if (upstream.status === 502 || upstream.status === 503) {
      await new Promise(r => setTimeout(r, 800));
      upstream = await fetchPdf();
    }
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
    // The backend already returns a JSON body like {"error": "..."}. Re-wrapping
    // that raw text as { error: raw } here double-encodes it into
    // {"error":"{\"error\":\"...\"}"} — which is what rendered as the garbled
    // nested-JSON blob on screen. Parse it through if it's already JSON;
    // only fall back to wrapping when the upstream body genuinely isn't JSON.
    let payload: unknown;
    try {
      const parsed = JSON.parse(raw);
      payload = (parsed && typeof parsed === 'object') ? parsed : { error: raw || `Upstream error (${upstream.status})` };
    } catch {
      payload = { error: raw || `Upstream error (${upstream.status})` };
    }
    log.error('PDF generation failed upstream: HTTP %d — %s', upstream.status, raw.slice(0, 120));
    done(upstream.status, 'upstream error');
    return NextResponse.json(payload, { status: upstream.status });
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
