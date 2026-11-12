import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';

// Provider finto: nessuna chiamata di rete durante i test
process.env.TRANSLATE_PROVIDER = 'mock';
process.env.STT_API_KEY = '';
process.env.TTS_API_KEY = '';
process.env.OPENAI_API_KEY = '';
process.env.APP_ACCESS_TOKEN = '';

const { createServer } = await import('../server/index.js');
const { createTagParser, buildPrompt } = await import('../server/providers/translate.js');
const { parseEnv } = await import('../server/config.js');
const { isHallucination } = await import('../server/providers/speech.js');

let server;
let base;

before(async () => {
  server = createServer();
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

test('GET /api/config descrive le funzionalità', async () => {
  const res = await fetch(`${base}/api/config`);
  const json = await res.json();
  assert.equal(json.translate.provider, 'mock');
  assert.equal(json.translate.ready, true);
  assert.equal(json.stt.ready, false);
});

test('POST /api/translate restituisce uno stream SSE con meta, delta e done', async () => {
  const res = await fetch(`${base}/api/translate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: 'hello world', from: 'en-US', to: 'it-IT' }),
  });
  assert.equal(res.headers.get('content-type'), 'text/event-stream; charset=utf-8');
  const body = await res.text();
  assert.match(body, /event: meta\ndata: {"detected":"en"}/);
  assert.match(body, /event: delta/);
  assert.match(body, /event: done/);
  const text = [...body.matchAll(/event: delta\ndata: (.*)/g)].map((m) => JSON.parse(m[1]).text).join('');
  assert.equal(text.trim(), '(it) hello world');
});

test('validazione input della traduzione', async () => {
  const res = await fetch(`${base}/api/translate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: 'x', from: '<script>', to: 'it' }),
  });
  assert.equal(res.status, 400);
});

test('STT non configurato → 503', async () => {
  const res = await fetch(`${base}/api/transcribe?langs=en-US`, {
    method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: new Uint8Array(2000),
  });
  assert.equal(res.status, 503);
});

test('file statici, manifest e protezione path traversal', async () => {
  const html = await fetch(`${base}/`);
  assert.equal(html.status, 200);
  assert.match(await html.text(), /manifest\.webmanifest/);
  const manifest = await fetch(`${base}/manifest.webmanifest`);
  assert.match(manifest.headers.get('content-type'), /manifest\+json/);
  const sw = await fetch(`${base}/sw.js`);
  assert.equal(sw.headers.get('cache-control'), 'no-cache');
  const evil = await fetch(`${base}/..%2f..%2fpackage.json`);
  assert.notEqual(evil.status, 200);
});

test('parser del tag lingua [[xx]] su chunk spezzati', () => {
  let tag = null;
  let out = '';
  const p = createTagParser((t) => { tag = t; }, (s) => { out += s; });
  for (const c of ['[', '[i', 't]]', '\n Ciao', ' mondo']) p.push(c);
  p.flush();
  assert.equal(tag, 'it');
  assert.equal(out, 'Ciao mondo');
});

test('parser del tag: risposta senza tag passa intatta', () => {
  let tag = null;
  let out = '';
  const p = createTagParser((t) => { tag = t; }, (s) => { out += s; });
  p.push('Hello');
  p.push(' there');
  p.flush();
  assert.equal(tag, null);
  assert.equal(out, 'Hello there');
});

test('prompt include contesto e istruzioni di rilevamento', () => {
  const { system, user } = buildPrompt({
    text: 'ciao', from: 'it-IT', to: 'en-US', context: [{ source: 'hi', translation: 'ciao' }],
  });
  assert.match(system, /\[\[it\]\]/);
  assert.match(system, /\[\[en\]\]/);
  assert.match(user, /hi → ciao/);
});

test('parseEnv e filtro allucinazioni Whisper', () => {
  assert.deepEqual(parseEnv('A=1\n# c\nB="x y"\nC=z # comm'), { A: '1', B: 'x y', C: 'z' });
  assert.ok(isHallucination('Thank you for watching!'));
  assert.ok(!isHallucination('Thank you for the coffee'));
});
