/**
 * ALFA – server HTTP (zero dipendenze).
 *
 * Responsabilità:
 *  1. Servire il frontend PWA statico (cartella /public).
 *  2. Fare da proxy sicuro verso le API di traduzione / STT / TTS,
 *     così le chiavi non finiscono mai nel browser.
 *
 * Endpoint:
 *  GET  /api/health      → stato del server
 *  GET  /api/config      → funzionalità disponibili (usato dal client all'avvio)
 *  POST /api/translate   → JSON {text, from, to, context?, detect?} → stream SSE
 *  POST /api/transcribe  → body audio grezzo (?langs=en-US,it-IT) → JSON {text, language}
 *  POST /api/tts         → JSON {text, lang, voice?} → audio/mpeg in streaming
 */
import http from 'node:http';
import { createReadStream, statSync } from 'node:fs';
import { extname, join, normalize, sep } from 'node:path';
import { Readable } from 'node:stream';
import { pathToFileURL } from 'node:url';
import { config, translateReady, translateDetects } from './config.js';
import {
  HttpError, readBody, readJson, sendJson, openSse, createRateLimiter, clientIp,
} from './lib/http.js';
import { translateStream } from './providers/translate.js';
import { transcribe, synthesize, sttReady, ttsReady } from './providers/speech.js';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};

// Header di sicurezza comuni. Il microfono è consentito solo all'origine stessa.
const SECURITY_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Permissions-Policy': 'microphone=(self), camera=(), geolocation=()',
  'Content-Security-Policy':
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob: data:; connect-src 'self'; worker-src 'self'; manifest-src 'self'",
};

// ─────────────────────────────────────────────────────────────────────────────
// API handlers
// ─────────────────────────────────────────────────────────────────────────────

/** Annulla le chiamate upstream se il client chiude la connessione. */
function abortOnClose(req, res) {
  const ctrl = new AbortController();
  res.on('close', () => { if (!res.writableFinished) ctrl.abort(); });
  req.on('aborted', () => ctrl.abort());
  return ctrl.signal;
}

const LANG_RE = /^[a-z]{2,3}([-_][A-Za-z0-9]{2,4})?$/;

async function handleTranslate(req, res) {
  const body = await readJson(req);
  const text = String(body.text || '').trim().slice(0, 2000);
  const { from, to } = body;
  if (!text) throw new HttpError(400, 'Testo mancante');
  if (!LANG_RE.test(from || '') || !LANG_RE.test(to || '')) throw new HttpError(400, 'Lingue non valide');
  if (!translateReady()) throw new HttpError(503, `Provider "${config.translate.provider}" non configurato`);

  const signal = abortOnClose(req, res);
  const context = Array.isArray(body.context)
    ? body.context.slice(-4).map((c) => ({
      source: String(c?.source || '').slice(0, 400),
      translation: String(c?.translation || '').slice(0, 400),
    }))
    : [];

  const send = openSse(res);
  try {
    await translateStream({
      text, from, to, context, signal,
      detect: body.detect !== false,
      onMeta: (meta) => send('meta', meta),
      onDelta: (t) => send('delta', { text: t }),
    });
    send('done', {});
  } catch (err) {
    if (!signal.aborted) send('error', { message: err.message || 'Errore di traduzione' });
  } finally {
    res.end();
  }
}

async function handleTranscribe(req, res, url) {
  const audio = await readBody(req, 8_000_000); // ~8 MB, ben oltre un segmento da 10 s
  if (audio.length < 1000) throw new HttpError(400, 'Audio troppo corto');
  const langs = (url.searchParams.get('langs') || '')
    .split(',').map((s) => s.trim()).filter((s) => LANG_RE.test(s)).slice(0, 2);
  const mime = String(req.headers['content-type'] || 'audio/wav').split(';')[0];
  const result = await transcribe({ audio, mime, langs, signal: abortOnClose(req, res) });
  sendJson(res, 200, result);
}

async function handleTts(req, res) {
  const body = await readJson(req);
  const text = String(body.text || '').trim().slice(0, 1000);
  if (!text) throw new HttpError(400, 'Testo mancante');
  const upstream = await synthesize({
    text,
    lang: String(body.lang || 'en'),
    voice: typeof body.voice === 'string' && /^[a-z_-]{2,20}$/i.test(body.voice) ? body.voice : '',
    signal: abortOnClose(req, res),
  });
  res.writeHead(200, {
    'Content-Type': upstream.headers.get('content-type') || 'audio/mpeg',
    'Cache-Control': 'no-store',
  });
  // Inoltra lo stream audio così com'è: la riproduzione può iniziare subito.
  Readable.fromWeb(upstream.body).pipe(res);
}

function handleConfig(res) {
  sendJson(res, 200, {
    translate: {
      provider: config.translate.provider,
      ready: translateReady(),
      detectsLanguage: translateDetects(),
    },
    stt: { ready: sttReady(), model: sttReady() ? config.stt.model : null },
    tts: { ready: ttsReady(), voice: config.tts.voice },
    requiresToken: Boolean(config.accessToken),
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Static files
// ─────────────────────────────────────────────────────────────────────────────

function serveStatic(req, res, url) {
  let pathname = decodeURIComponent(url.pathname);
  if (pathname.endsWith('/')) pathname += 'index.html';
  const filePath = normalize(join(config.publicDir, pathname));
  // Protezione path traversal
  if (!filePath.startsWith(config.publicDir + sep)) throw new HttpError(403, 'Vietato');

  let stat;
  try {
    stat = statSync(filePath);
    if (stat.isDirectory()) return serveStatic(req, res, new URL(`${url.pathname}/`, url));
  } catch {
    // Fallback SPA: le navigazioni sconosciute ricevono index.html
    if ((req.headers.accept || '').includes('text/html')) {
      return serveStatic(req, res, new URL('/', url));
    }
    throw new HttpError(404, 'Non trovato');
  }

  const ext = extname(filePath);
  // Service worker e HTML sempre rivalidati, il resto cache breve (il SW gestisce l'offline).
  const noCache = ext === '.html' || pathname === '/sw.js' || ext === '.webmanifest';
  res.writeHead(200, {
    'Content-Type': MIME[ext] || 'application/octet-stream',
    'Content-Length': stat.size,
    'Cache-Control': noCache ? 'no-cache' : 'public, max-age=3600',
    ...(pathname === '/sw.js' ? { 'Service-Worker-Allowed': '/' } : {}),
  });
  if (req.method === 'HEAD') return res.end();
  createReadStream(filePath).pipe(res);
}

// ─────────────────────────────────────────────────────────────────────────────
// Router
// ─────────────────────────────────────────────────────────────────────────────

export function createServer() {
  const allow = createRateLimiter(config.rateLimitPerMin);

  return http.createServer(async (req, res) => {
    for (const [k, v] of Object.entries(SECURITY_HEADERS)) res.setHeader(k, v);
    const url = new URL(req.url, 'http://localhost');

    try {
      if (url.pathname.startsWith('/api/')) {
        if (url.pathname === '/api/health') return sendJson(res, 200, { ok: true });

        if (!allow(clientIp(req))) throw new HttpError(429, 'Troppe richieste, riprova tra poco');
        if (config.accessToken && url.pathname !== '/api/config'
          && req.headers['x-app-token'] !== config.accessToken) {
          throw new HttpError(401, 'Token di accesso mancante o errato');
        }

        if (req.method === 'GET' && url.pathname === '/api/config') return handleConfig(res);
        if (req.method === 'POST' && url.pathname === '/api/translate') return await handleTranslate(req, res);
        if (req.method === 'POST' && url.pathname === '/api/transcribe') return await handleTranscribe(req, res, url);
        if (req.method === 'POST' && url.pathname === '/api/tts') return await handleTts(req, res);
        throw new HttpError(404, 'Endpoint inesistente');
      }

      if (req.method !== 'GET' && req.method !== 'HEAD') throw new HttpError(405, 'Metodo non consentito');
      return serveStatic(req, res, url);
    } catch (err) {
      const status = err.status || 500;
      if (status >= 500) console.error(`[${req.method} ${url.pathname}]`, err.message);
      if (res.headersSent) return res.end();
      sendJson(res, status, { error: err.message || 'Errore interno' });
    }
  });
}

// Avvio diretto (`node server/index.js`), non quando importato dai test.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  createServer().listen(config.port, config.host, () => {
    console.log(`ALFA in ascolto su http://localhost:${config.port}`);
    console.log(`  traduzione: ${config.translate.provider} (${translateReady() ? 'pronto' : 'NON configurato'})`);
    console.log(`  STT cloud:  ${sttReady() ? config.stt.model : 'disattivato'}`);
    console.log(`  TTS cloud:  ${ttsReady() ? config.tts.model : 'disattivato'}`);
  });
}
