function required(name: string): string {
  const value = process.env[name];
  if (!value || value.trim() === '') {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}

function optional(name: string, fallback: string): string {
  const value = process.env[name];
  return value && value.trim() !== '' ? value : fallback;
}

function optionalInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export interface AppConfig {
  port: number;
  logLevel: string;
  /** Shared secret Apex includes in the Authorization header. */
  apiKey: string;
  /** Per-instance max concurrent PDF workers. Tune for box size. */
  workerConcurrency: number;
  /** Reject inbound PDFs larger than this (defensive). */
  maxSourceBytes: number;
}

export function loadConfig(): AppConfig {
  return {
    port: optionalInt('PORT', 8080),
    logLevel: optional('LOG_LEVEL', 'info'),
    apiKey: required('PDF_SERVICE_API_KEY'),
    workerConcurrency: optionalInt('WORKER_CONCURRENCY', 2),
    maxSourceBytes: optionalInt('MAX_SOURCE_BYTES', 100 * 1024 * 1024), // 100 MB
  };
}
