/**
 * Thin proxy → Python FastAPI backend /api/chat/report/html
 * Returns a self-contained animated/interactive HTML document (text/html).
 *
 * Mirrors ../pdf/route.ts (same body shape, same backend host) — this is
 * a second RENDERER for the same report payload, not a second
 * generation step. See backend/routes/html_report.py for why this is a
 * separate route from /report/pdf.
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';
import { LOGO_B64 } from '../pdf/logos';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 120;

const log = createLogger('api/chat/report/html');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/chat/report/html');

  let bodyObj: Record<string, unknown>;
  try {
    bodyObj = await req.json();
  } catch {
    done(400, 'invalid json');
    return NextResponse.json({ error: 'Invalid request body' }, { status: 400 });
  }

  // Same logo injection as the PDF proxy, for parity — harmless no-op if
  // the backend renderer doesn't currently read it.
  bodyObj.logoB64 = LOGO_B64;

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/api/chat/report/html`, {
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
    const raw = await upstream.text();
    let payload: unknown;
    try {
      const parsed = JSON.parse(raw);
      payload = (parsed && typeof parsed === 'object') ? parsed : { error: raw || `Upstream error (${upstream.status})` };
    } catch {
      payload = { error: raw || `Upstream error (${upstream.status})` };
    }
    log.error('HTML report generation failed upstream: HTTP %d — %s', upstream.status, raw.slice(0, 120));
    done(upstream.status, 'upstream error');
    return NextResponse.json(payload, { status: upstream.status });
  }

  const htmlText = await upstream.text();
  const dateStr = new Date().toISOString().slice(0, 10);
  done(200, `${(htmlText.length / 1024).toFixed(1)} KB`);
  return new NextResponse(htmlText, {
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      // "attachment" (not "inline") so this behaves like the PDF download —
      // a self-contained file the user saves and can reopen/share, rather
      // than replacing the current tab. It's a single HTML file with all
      // CSS/JS/Chart.js inline (see html_report.py), so it opens correctly
      // from disk with no external dependencies.
      'Content-Disposition': `attachment; filename="growth-gradual-report-${dateStr}.html"`,
      'Cache-Control': 'no-store',
    },
  });
}
