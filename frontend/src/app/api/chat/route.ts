/**
 * Thin proxy → Python FastAPI backend /api/chat
 * Set BACKEND_URL env var (e.g. https://your-backend.onrender.com)
 */
import { NextRequest } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 120; // 2 min — covers slow LLM streaming responses

const log = createLogger('api/chat');
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/chat');
  const body = await req.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      // @ts-expect-error — Node 18 fetch supports duplex
      duplex: 'half',
    });
  } catch (err) {
    log.error('Backend unreachable at %s — %s', BACKEND, err);
    done(502, 'backend unreachable');
    return new Response(JSON.stringify({ error: 'Backend unavailable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  done(upstream.status, upstream.ok ? 'streaming' : 'upstream error');
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      'Content-Type': upstream.headers.get('Content-Type') ?? 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
    },
  });
}
