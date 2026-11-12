import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';

// Finto endpoint Whisper: il comportamento dipende dal campo "language" del form
let mode = 'wrong-lang';
const upstream = http.createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const body = Buffer.concat(chunks).toString('latin1');
  const lang = /name="language"\r\n\r\n(\w+)/.exec(body)?.[1] || null;
  const reply = (text, language, logprob) => res.end(JSON.stringify({
    text, language, duration: 1.2, segments: [{ text, avg_logprob: logprob, no_speech_prob: 0.05 }],
  }));
  res.setHeader('Content-Type', 'application/json');
  if (!lang) return mode === 'in-pair' ? reply('hello there', 'english', -0.2) : reply('¿Qué?', 'spanish', -0.6);
  if (mode === 'noise') return reply(lang === 'it' ? 'eh' : 'uh', lang, -1.6);
  if (lang === 'it') return reply('Che cosa?', 'italian', -0.3);
  return reply('Ke?', 'english', -0.9);
});

let transcribe;
before(async () => {
  await new Promise((r) => upstream.listen(0, '127.0.0.1', r));
  process.env.STT_BASE_URL = `http://127.0.0.1:${upstream.address().port}`;
  process.env.STT_API_KEY = 'test';
  process.env.STT_MODEL = 'whisper-large-v3-turbo';
  ({ transcribe } = await import('../server/providers/speech.js'));
});
after(() => upstream.close());

const audio = Buffer.alloc(4000);

test('lingua rilevata nella coppia: un solo passaggio', async () => {
  mode = 'in-pair';
  const r = await transcribe({ audio, mime: 'audio/wav', langs: ['en-US', 'it-IT'] });
  assert.deepEqual([r.text, r.language], ['hello there', 'en']);
});

test('lingua fuori coppia (spagnolo): si forza la lingua più affidabile', async () => {
  mode = 'wrong-lang';
  const r = await transcribe({ audio, mime: 'audio/wav', langs: ['en-US', 'it-IT'] });
  assert.deepEqual([r.text, r.language], ['Che cosa?', 'it']);
});

test('fuori coppia e poco affidabile: scartato come rumore', async () => {
  mode = 'noise';
  const r = await transcribe({ audio, mime: 'audio/wav', langs: ['en-US', 'it-IT'] });
  assert.equal(r.text, '');
});
