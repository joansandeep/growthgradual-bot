import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL ?? '';
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? '';

if (!supabaseUrl || !supabaseAnonKey) {
  // Non-fatal: lets the app still build/run without auth configured,
  // but auth calls will fail loudly until the env vars are set.
  console.warn(
    '[supabase] NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY are not set.'
  );
}

// createClient() throws on an empty URL, which would crash the Next.js
// build/prerender step (it evaluates this module even for pages that never
// call it). Fall back to a syntactically-valid placeholder so the build
// always succeeds — real auth calls will just fail until the real env vars
// are set on the deploy host.
export const supabase = createClient(
  supabaseUrl || 'https://placeholder.supabase.co',
  supabaseAnonKey || 'placeholder-anon-key',
  {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  }
);
