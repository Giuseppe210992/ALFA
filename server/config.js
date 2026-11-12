/**
 * Configurazione del backend.
 *
 * Carica `.env` (se presente) con un parser minimale, così il progetto non ha
 * dipendenze npm. Le variabili già presenti nell'ambiente hanno la precedenza
 * su quelle del file (comportamento standard di dotenv).
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/** Parser .env: KEY=VALUE, commenti con #, valori opzionalmente tra virgolette. */
export function parseEnv(text) {
  const out = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    } else {
      // Rimuove i commenti in coda solo per valori non quotati
      const hash = value.indexOf(' #');
      if (hash !== -1) value = value.slice(0, hash).trim();
    }
    out[key] = value;
  }
  return out;
}

function loadDotEnv() {
  const file = join(ROOT, '.env');
  if (!existsSync(file)) return;
  const parsed = parseEnv(readFileSync(file, 'utf8'));
  for (const [k, v] of Object.entries(parsed)) {
    if (process.env[k] === undefined) process.env[k] = v;
  }
}

loadDotEnv();

const env = (key, fallback = '') => (process.env[key] ?? '').trim() || fallback;
const openaiKey = env('OPENAI_API_KEY');

export const config = {
  root: ROOT,
  publicDir: join(ROOT, 'public'),
  port: Number(env('PORT', '3000')),
  host: env('HOST', '0.0.0.0'),
  accessToken: env('APP_ACCESS_TOKEN'),
  rateLimitPerMin: Number(env('RATE_LIMIT_PER_MIN', '240')),

  translate: {
    provider: env('TRANSLATE_PROVIDER', 'mymemory').toLowerCase(),
    openai: {
      baseUrl: env('LLM_BASE_URL', 'https://api.openai.com/v1').replace(/\/$/, ''),
      apiKey: env('LLM_API_KEY', openaiKey),
      model: env('LLM_MODEL', 'gpt-4o-mini'),
    },
    anthropic: {
      apiKey: env('ANTHROPIC_API_KEY'),
      model: env('ANTHROPIC_MODEL', 'claude-haiku-5-5'),
    },
    deepl: {
      apiKey: env('DEEPL_API_KEY'),
      baseUrl: env('DEEPL_BASE_URL', 'https://api-free.deepl.com').replace(/\/$/, ''),
    },
    mymemory: { email: env('MYMEMORY_EMAIL') },
  },

  stt: {
    baseUrl: env('STT_BASE_URL', 'https://api.openai.com/v1').replace(/\/$/, ''),
    apiKey: env('STT_API_KEY', openaiKey),
    model: env('STT_MODEL', 'whisper-1'),
  },

  tts: {
    baseUrl: env('TTS_BASE_URL', 'https://api.openai.com/v1').replace(/\/$/, ''),
    apiKey: env('TTS_API_KEY', openaiKey),
    model: env('TTS_MODEL', 'gpt-4o-mini-tts'),
    voice: env('TTS_VOICE', 'alloy'),
  },
};

/** Il provider di traduzione scelto è utilizzabile? (chiave presente, ecc.) */
export function translateReady() {
  const t = config.translate;
  switch (t.provider) {
    case 'openai': return Boolean(t.openai.apiKey) || /localhost|127\.0\.0\.1/.test(t.openai.baseUrl);
    case 'anthropic': return Boolean(t.anthropic.apiKey);
    case 'deepl': return Boolean(t.deepl.apiKey);
    case 'mymemory':
    case 'mock': return true;
    default: return false;
  }
}

/** Il provider supporta il rilevamento della lingua (tag [[xx]] dell'LLM)? */
export function translateDetects() {
  return ['openai', 'anthropic', 'mock'].includes(config.translate.provider);
}
