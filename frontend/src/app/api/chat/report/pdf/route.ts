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
export const maxDuration = 120;

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
      signal: AbortSignal.timeout(165_000),
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
    const contentType = (upstream.headers.get('content-type') ?? '').toLowerCase();
    const raw = await upstream.text();
    const trimmed = raw.trim();
    const looksLikeHtml = /<!doctype\s+html|<html[\s>]|<head[\s>]/i.test(trimmed) || contentType.includes('text/html');
    let message = `PDF generation failed (HTTP ${upstream.status}).`;

    // Hosting/framework layers can return a complete HTML error page. Never
    // surface that document in the report UI; convert it to one safe sentence.
    if (looksLikeHtml) {
      message = upstream.status >= 500
        ? 'The PDF service returned a server error. Please retry the PDF export.'
        : `PDF generation failed (HTTP ${upstream.status}).`;
    } else {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed && typeof parsed === 'object' && typeof (parsed as any).error === 'string' && (parsed as any).error.trim()) {
          message = (parsed as any).error;
        } else if (trimmed) {
          message = trimmed.slice(0, 500);
        }
      } catch {
        if (trimmed) message = trimmed.slice(0, 500);
      }
    }

    log.error('PDF generation failed upstream: HTTP %d (%s) — %s', upstream.status, contentType || 'unknown', looksLikeHtml ? '<html error page suppressed>' : trimmed.slice(0, 120));
    done(upstream.status, 'upstream error');
    return NextResponse.json({ error: message }, { status: upstream.status });
  }

  const contentType = (upstream.headers.get('content-type') ?? '').toLowerCase();
  const pdfBuffer = await upstream.arrayBuffer();
  const pdfBytes = new Uint8Array(pdfBuffer);
  const signature = new TextDecoder().decode(pdfBytes.slice(0, 5));
  if (!contentType.includes('application/pdf') || signature !== '%PDF-') {
    // A 2xx response can still be a framework/edge HTML page. Do not let it
    // masquerade as a PDF download.
    log.error('PDF upstream returned non-PDF success response: content-type=%s signature=%s bytes=%d', contentType || 'unknown', signature, pdfBytes.length);
    done(502, 'non-pdf upstream response');
    return NextResponse.json({ error: 'The PDF service returned an unexpected response. Please retry the PDF export.' }, { status: 502 });
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
