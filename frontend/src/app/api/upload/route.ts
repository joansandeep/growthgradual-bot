/**
 * /api/upload — Growth Gradual PDF upload proxy
 *
 * Forwards multipart file uploads to the Paperly API gateway, which:
 *   1. Stores the file in Supabase via file-service (Render)
 *   2. Extracts text / OCRs pages via extraction-service (Render + Gemini Vision)
 *   3. Chunks + embeds into FAISS via rag-service (HuggingFace Space)
 *
 * Client sends:   POST /api/upload  multipart: files[], sessionId
 * Gateway URL:    PAPERLY_GATEWAY_URL/api/files/upload/:sessionId
 */

import { NextRequest, NextResponse } from 'next/server';
import { createLogger, logRequest } from '@/lib/logger';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const log = createLogger('api/upload');
const GATEWAY = (process.env.PAPERLY_GATEWAY_URL ?? 'https://paperly-api-gateway-j9do.onrender.com').replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const done = logRequest(log, 'POST', '/api/upload');
  try {
    const formData = await req.formData();
    const sessionId = (formData.get('sessionId') as string | null)?.trim();
    if (!sessionId) {
      done(400, 'missing sessionId');
      return NextResponse.json({ error: 'sessionId is required' }, { status: 400 });
    }

    // Ensure session exists on Paperly backend
    try {
      await fetch(`${GATEWAY}/api/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId }),
        signal: AbortSignal.timeout(10_000),
      });
    } catch {
      // Non-fatal — file-service upserts the session anyway
    }

    // Rebuild FormData to forward to Paperly gateway
    const upstream = new FormData();
    const files = formData.getAll('files') as File[];
    if (!files.length) {
      done(400, 'no files');
      return NextResponse.json({ error: 'No files provided' }, { status: 400 });
    }
    for (const f of files) {
      upstream.append('files', f, f.name);
    }

    log.info('Uploading %d file(s) for session %s', files.length, sessionId);
    const res = await fetch(`${GATEWAY}/api/files/upload/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      body: upstream,
      signal: AbortSignal.timeout(300_000), // 5 min — OCR can be slow
    });

    const data = await res.json().catch(() => ({ error: 'Bad gateway response' }));
    done(res.ok ? 200 : res.status, `session=${sessionId}`);
    return NextResponse.json(data, { status: res.ok ? 200 : res.status });
  } catch (err) {
    log.error('Upload handler error: %s', err);
    done(502, 'exception');
    return NextResponse.json(
      { error: err instanceof Error ? err.message : 'Upload failed' },
      { status: 502 },
    );
  }
}
