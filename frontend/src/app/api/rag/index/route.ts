import { NextRequest, NextResponse } from 'next/server';
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');
const RAG_URL = (process.env.RAG_SERVICE_URL ?? 'https://sandy31-paperly-rag-service.hf.space').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();

    // Pre-warm the HF Space before the real index call.
    // Free-tier Spaces sleep after ~15 min — a cold start takes 30-90s and will
    // cause the index to time out. We ping first (up to 90s) so the model is loaded.
    try {
      await fetch(`${RAG_URL}/ping`, { signal: AbortSignal.timeout(90_000) });
    } catch {
      // Space unreachable or still cold — proceed anyway, the index call will wake it
    }

    const res = await fetch(`${BACKEND}/api/rag/index`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(120_000), // 2 min timeout for large PDFs
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    return NextResponse.json({ error: 'RAG index proxy failed', chunks_added: 0 }, { status: 502 });
  }
}
