import { NextRequest, NextResponse } from 'next/server';
import { createLogger } from '@/lib/logger';

const log = createLogger('api/query');

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// ─── Groq API key pool with rate-limit rotation ───────────────────────────────
// Keys are tried in order; if one hits a 429 (rate limit), the next is used.
// All 4 keys are added to the pool — exhausted keys rotate back after 60s.

const GROQ_KEYS = (process.env.GROQ_API_KEYS || '').split(',').map(k => k.trim()).filter(Boolean);

// Track which keys are rate-limited and when they can be retried
const rateLimitedUntil: Record<string, number> = {};
const RATE_LIMIT_BACKOFF_MS = 60_000; // 60 seconds before retrying a rate-limited key

function getAvailableKeys(): string[] {
  const now = Date.now();
  const available = GROQ_KEYS.filter(k => !rateLimitedUntil[k] || rateLimitedUntil[k] < now);
  if (available.length === 0) {
    // All keys exhausted — return the one with the soonest retry time
    const soonest = GROQ_KEYS.reduce((a, b) =>
      (rateLimitedUntil[a] ?? 0) < (rateLimitedUntil[b] ?? 0) ? a : b
    );
    return [soonest];
  }
  return available;
}

async function callGroq(question: string): Promise<string> {
  const keys = getAvailableKeys();
  let lastError: Error | null = null;

  for (const key of keys) {
    try {
      const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${key}`,
        },
        body: JSON.stringify({
          model: 'llama-3.3-70b-versatile',
          max_tokens: 1000,
          messages: [
            {
              role: 'system',
              content: `You are Growth Gradual — In The Money, an expert Indian financial markets assistant. Today is ${new Date().toDateString()}.

Your platform aggregates live news from 65+ sources: NSE, BSE, RBI, Moneycontrol, Economic Times, Livemint, Reuters India, Morningstar, AMFI, Business Standard, CNBC TV18, Zee Business, and many more.

When asked about latest news, today's market, or current events — summarise the most relevant recent headlines with source attribution. Be specific, not generic.

Format: **bold headline**, then 2-3 bullet points, then a 1-2 sentence summary. Focus on Indian markets. Today's date: ${new Date().toDateString()}.`,
            },
            { role: 'user', content: question },
          ],
        }),
        signal: AbortSignal.timeout(30_000),
      });

      if (res.status === 429) {
        // Rate limited — mark this key and try the next one
        rateLimitedUntil[key] = Date.now() + RATE_LIMIT_BACKOFF_MS;
        log.warn('Groq key ...%s rate limited — rotating', key.slice(-8));
        lastError = new Error(`Rate limited on key ...${key.slice(-8)}`);
        continue;
      }

      if (!res.ok) {
        const errBody = await res.text().catch(() => '');
        throw new Error(`Groq API error ${res.status}: ${errBody}`);
      }

      const data = await res.json();
      const text = data.choices?.[0]?.message?.content || 'No response from Groq.';
      return text;

    } catch (err) {
      if (err instanceof Error && err.message.includes('Rate limited')) {
        // Already handled above, continue to next key
        continue;
      }
      lastError = err instanceof Error ? err : new Error(String(err));
      log.error('Groq key ...%s failed: %s', key.slice(-8), lastError.message);
      // For non-rate-limit errors, try next key as well
      continue;
    }
  }

  throw lastError ?? new Error('All Groq API keys exhausted or failed');
}

export async function POST(req: NextRequest) {
  const { question } = await req.json();
  if (!question?.trim()) {
    return NextResponse.json({ error: 'No question provided' }, { status: 400 });
  }

  try {
    const answer = await callGroq(question.trim());
    return NextResponse.json({ answer });
  } catch (err) {
    log.error('Query handler error: %s', err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : 'AI query failed' },
      { status: 502 }
    );
  }
}
