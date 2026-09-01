/**
 * Thin proxy → Python FastAPI backend /api/stocks/{id}/export.xlsx
 *
 * Serves the screener-fundamentals "source of truth" workbook for a
 * company matched by the chat KB lookup (see routes/chat.py `kbCompany`
 * in the SSE meta event), so the browser can download it directly from
 * the same origin as the rest of the app.
 */
import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const log = createLogger('api/stocks/export');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function GET(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const done = logRequest(log, 'GET', `/api/stocks/${id}/export`);

  if (!/^\d+$/.test(id)) {
    done(400, 'invalid id');
    return NextResponse.json({ error: 'Invalid company id' }, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/api/stocks/${id}/export.xlsx`);
  } catch (err) {
    log.error('Backend unreachable at %s — %s', BACKEND, err);
    done(502, 'backend unreachable');
    return NextResponse.json({ error: 'Backend unavailable' }, { status: 502 });
  }

  if (!upstream.ok) {
    done(upstream.status, 'upstream error');
    const errBody = await upstream.text().catch(() => '');
    return NextResponse.json({ error: errBody || 'Export failed' }, { status: upstream.status });
  }

  done(200, 'ok');
  const buf = await upstream.arrayBuffer();
  return new NextResponse(buf, {
    status: 200,
    headers: {
      'Content-Type': upstream.headers.get('content-type')
        ?? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'Content-Disposition': upstream.headers.get('content-disposition') ?? 'attachment',
    },
  });
}
