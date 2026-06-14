import { NextRequest, NextResponse } from 'next/server';
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
const BACKEND = (process.env.BACKEND_URL ?? 'http://localhost:8000').replace(/\/$/, '');
export async function DELETE(req: NextRequest, { params }: { params: { session_id: string } }) {
  try {
    const res = await fetch(`${BACKEND}/api/rag/session/${params.session_id}`, { method: 'DELETE' });
    return NextResponse.json({ success: true }, { status: res.status });
  } catch {
    return NextResponse.json({ success: false }, { status: 502 });
  }
}
