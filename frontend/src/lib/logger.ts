/**
 * Lightweight structured logger for Growth Gradual Next.js API routes.
 * All output goes to stdout/stderr — visible in `npm run dev` terminal.
 *
 * Format:  HH:MM:SS [GG-Frontend] LEVEL  [scope] — message
 */

type Level = 'INFO' | 'WARN' | 'ERROR' | 'DEBUG';

function timestamp(): string {
  return new Date().toTimeString().slice(0, 8); // HH:MM:SS
}

function write(level: Level, scope: string, msg: string, ...args: unknown[]) {
  const prefix = `${timestamp()} [GG-Frontend] ${level.padEnd(5)}  [${scope}]`;
  const formatted = args.length
    ? `${msg} ${args.map(a => (typeof a === 'object' ? JSON.stringify(a) : String(a))).join(' ')}`
    : msg;
  const line = `${prefix} — ${formatted}`;

  if (level === 'ERROR') {
    console.error(line);
  } else if (level === 'WARN') {
    console.warn(line);
  } else {
    console.log(line);
  }
}

export function createLogger(scope: string) {
  return {
    info:  (msg: string, ...args: unknown[]) => write('INFO',  scope, msg, ...args),
    warn:  (msg: string, ...args: unknown[]) => write('WARN',  scope, msg, ...args),
    error: (msg: string, ...args: unknown[]) => write('ERROR', scope, msg, ...args),
    debug: (msg: string, ...args: unknown[]) => {
      if (process.env.LOG_LEVEL === 'debug') write('DEBUG', scope, msg, ...args);
    },
  };
}

/** Log an incoming API request and return a helper that logs the response. */
export function logRequest(
  logger: ReturnType<typeof createLogger>,
  method: string,
  path: string,
) {
  const t0 = performance.now();
  logger.info(`→ ${method} ${path}`);
  return function logResponse(status: number, note?: string) {
    const ms = (performance.now() - t0).toFixed(0);
    const suffix = note ? `  ${note}` : '';
    logger.info(`← ${method} ${path}  ${status}  ${ms}ms${suffix}`);
  };
}
