/**
 * POST /api/chat/report/email
 * Thin proxy → Python FastAPI /api/chat/report/email
 * Forwards multipart/form-data (subject, recipients, file, report, title, summary, keyStats)
 */
import { NextRequest, NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 60;

const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  try {
    // Forward the raw multipart form-data body unchanged
    const body = await req.arrayBuffer();
    const contentType = req.headers.get('content-type') ?? '';

    const res = await fetch(`${BACKEND}/api/chat/report/email`, {
      method: 'POST',
      headers: { 'content-type': contentType },
      body,
    });

    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    console.error('[api/chat/report/email] proxy error:', err);
    return NextResponse.json({ success: false, error: 'Failed to reach backend.' }, { status: 502 });
  }
}
