/**
 * Thin proxy → Python FastAPI backend /api/chat/report/edit
 * Body: { report: string, editInstruction: string, title?: string }
 * Edits ONE section of an already-generated report and returns the full
 * report text with only that section changed — see backend/routes/report.py
 * (edit_report_section) for how the target section is located and spliced.
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 60;

const log = createLogger('api/chat/report/edit');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/chat/report/edit');
  const body = await req.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/api/chat/report/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: AbortSignal.timeout(55_000),
    });
  } catch (err) {
    const timedOut = err instanceof Error && (err.name === 'TimeoutError' || err.name === 'AbortError');
    log.error('Backend unreachable or timed out: %s', err);
    done(timedOut ? 504 : 502, timedOut ? 'backend timeout' : 'backend unreachable');
    return NextResponse.json(
      { error: timedOut
        ? 'The edit took too long and timed out. Please try again.'
        : 'Backend unavailable' },
      { status: timedOut ? 504 : 502 },
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

  const editedSection = (typeof data === 'object' && data && 'editedSection' in data)
    ? String((data as Record<string, unknown>).editedSection ?? '').slice(0, 60)
    : '';
  done(upstream.status, `section="${editedSection}"`);
  return NextResponse.json(data, { status: upstream.status });
}
