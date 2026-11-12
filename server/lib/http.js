/**
 * Utility HTTP condivise dal server: lettura body con limite, risposte JSON,
 * Server-Sent Events, rate limiting e parsing degli stream SSE degli upstream.
 */

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** Legge l'intero body della richiesta come Buffer, rifiutando oltre `limit` byte. */
export function readBody(req, limit = 1_000_000) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new HttpError(413, 'Payload troppo grande'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

export async function readJson(req, limit = 64_000) {
  const buf = await readBody(req, limit);
  try {
    return JSON.parse(buf.toString('utf8') || '{}');
  } catch {
    throw new HttpError(400, 'JSON non valido');
  }
}

export function sendJson(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

/** Apre una risposta Server-Sent Events e restituisce una funzione `send(event, data)`. */
export function openSse(res) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-store, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no', // disattiva il buffering di nginx/proxy
  });
  res.flushHeaders?.();
  return (event, data) => {
    if (res.writableEnded) return;
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };
}

/**
 * Itera gli eventi SSE di una Response fetch (stream degli LLM).
 * Restituisce oggetti { event, data } dove data è la stringa grezza.
 */
export async function* iterateSse(response) {
  const decoder = new TextDecoder();
  let buffer = '';
  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true });
    let sep;
    // Gli eventi SSE sono separati da una riga vuota
    while ((sep = buffer.search(/\r?\n\r?\n/)) !== -1) {
      const rawEvent = buffer.slice(0, sep);
      buffer = buffer.slice(sep).replace(/^\r?\n\r?\n/, '');
      let event = 'message';
      const dataLines = [];
      for (const line of rawEvent.split(/\r?\n/)) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
      }
      if (dataLines.length) yield { event, data: dataLines.join('\n') };
    }
  }
}

/** Rate limiter "fixed window" in memoria, per IP. Sufficiente per un'istanza singola. */
export function createRateLimiter(perMinute) {
  const hits = new Map();
  setInterval(() => hits.clear(), 60_000).unref();
  return (ip) => {
    if (!perMinute) return true;
    const n = (hits.get(ip) || 0) + 1;
    hits.set(ip, n);
    return n <= perMinute;
  };
}

export function clientIp(req) {
  const fwd = req.headers['x-forwarded-for'];
  if (typeof fwd === 'string' && fwd) return fwd.split(',')[0].trim();
  return req.socket.remoteAddress || 'unknown';
}

/** Converte un errore di un upstream (fetch) in un messaggio leggibile. */
export async function upstreamError(response, label) {
  let detail = '';
  try {
    detail = (await response.text()).slice(0, 300);
  } catch { /* ignora */ }
  return new HttpError(502, `${label}: HTTP ${response.status} ${detail}`.trim());
}
