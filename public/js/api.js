/**
 * Client delle API del backend. Aggiunge il token di accesso (se configurato)
 * e converte gli errori HTTP in eccezioni leggibili.
 */
let accessToken = '';

export const setAccessToken = (t) => { accessToken = t || ''; };

export async function apiFetch(path, init = {}) {
  const headers = new Headers(init.headers || {});
  if (accessToken) headers.set('x-app-token', accessToken);
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch { /* non JSON */ }
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return res;
}

export async function getServerConfig() {
  try {
    return await (await apiFetch('/api/config')).json();
  } catch {
    return null; // offline o server irraggiungibile
  }
}

/**
 * Traduzione in streaming (SSE su POST).
 * @returns {Promise<void>} risolta a fine stream
 */
export async function translateStream({ text, from, to, context, detect, signal, onMeta, onDelta }) {
  const res = await apiFetch('/api/translate', {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, from, to, context, detect }),
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const event = /^event: (.*)$/m.exec(raw)?.[1];
      const dataLine = /^data: (.*)$/m.exec(raw)?.[1];
      if (!event || dataLine === undefined) continue;
      const data = JSON.parse(dataLine);
      if (event === 'meta') onMeta?.(data);
      else if (event === 'delta') onDelta?.(data.text);
      else if (event === 'error') throw new Error(data.message);
      else if (event === 'done') return;
    }
  }
}

export async function transcribe(wavBuffer, langs, signal) {
  const res = await apiFetch(`/api/transcribe?langs=${encodeURIComponent(langs.join(','))}`, {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'audio/wav' },
    body: wavBuffer,
  });
  return res.json();
}

export async function synthesize(text, lang, voice, signal) {
  const res = await apiFetch('/api/tts', {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, lang, voice }),
  });
  return res.blob();
}
