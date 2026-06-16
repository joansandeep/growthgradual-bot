import { NextRequest, NextResponse } from 'next/server';
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 300; // 5 min — covers HF cold start (up to 90s) + indexing

const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');
const RAG_URL = (process.env.RAG_SERVICE_URL ?? 'https://sandy31-paperly-rag-service.hf.space').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();

    // Fire ping and index in PARALLEL — don't wait for ping before indexing.
    // The ping wakes the HF Space; the index call will also wake it naturally.
    // If the space is cold both will wait ~60-90s for the cold start together,
    // rather than ping waiting 90s first THEN index waiting another 120s.
    const pingPromise = fetch(`${RAG_URL}/ping`, {
      signal: AbortSignal.timeout(120_000),
    }).catch(() => null); // non-fatal — just for warming

    const indexPromise = fetch(`${BACKEND}/api/rag/index`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(270_000), // 4.5 min — generous for cold HF Space
    });

    // We only need the index result; let ping finish in background
    const [res] = await Promise.all([indexPromise, pingPromise]);
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    return NextResponse.json({ error: 'RAG index proxy failed', chunks_added: 0 }, { status: 502 });
  }
}
