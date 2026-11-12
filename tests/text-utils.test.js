import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createSentenceSplitter, similarity, normalizeText } from '../public/js/text-utils.js';
import { Vad, encodeWav, downsample } from '../public/js/stt/audio-utils.js';

function split(chunks, opts) {
  const out = [];
  const s = createSentenceSplitter((x) => out.push(x), opts);
  for (const c of chunks) s.push(c);
  s.flush();
  return out;
}

test('splitter: emette le frasi appena complete, anche in streaming', () => {
  const out = [];
  const s = createSentenceSplitter((x) => out.push(x));
  s.push('Ciao, come ');
  s.push('stai? Io bene');
  assert.deepEqual(out, ['Ciao, come stai?']);
  s.push('. Grazie');
  s.flush();
  assert.deepEqual(out, ['Ciao, come stai?', 'Io bene.', 'Grazie']);
});

test('splitter: non spezza i numeri decimali', () => {
  assert.deepEqual(split(['Costa 3', '.5 euro. Ok']), ['Costa 3.5 euro.', 'Ok']);
});

test('splitter: frasi lunghe senza punto tagliate sulla virgola', () => {
  const long = 'Questa è una frase molto lunga che continua senza fermarsi mai, perché chi parla non fa pause e va avanti a lungo';
  const out = split([long], { softMax: 60, hardMax: 120 });
  assert.ok(out.length >= 2);
  assert.ok(out[0].endsWith(','));
});

test('splitter: ignora frammenti di sola punteggiatura', () => {
  assert.deepEqual(split(['Sì. . ']), ['Sì.']);
});

test('similarity/normalize per il filtro anti-eco', () => {
  assert.equal(normalizeText('Perché, sì!'), 'perche si');
  assert.ok(similarity('Hello, how are you?', 'hello how are you') > 0.9);
  assert.ok(similarity('Hello there', 'Buongiorno a tutti') < 0.1);
});

test('VAD: rileva inizio e fine del parlato', () => {
  const vad = new Vad({ startMs: 80, hangoverMs: 400 });
  for (let i = 0; i < 20; i++) assert.equal(vad.feed(0.003, 40), null); // rumore di fondo
  let ev = null;
  for (let i = 0; i < 5 && !ev; i++) ev = vad.feed(0.2, 40);
  assert.equal(ev, 'start');
  ev = null;
  for (let i = 0; i < 20 && !ev; i++) ev = vad.feed(0.003, 40);
  assert.equal(ev, 'end');
});

test('WAV: header corretto e ricampionamento', () => {
  const pcm = downsample(new Float32Array(48000).fill(0.5), 48000, 16000);
  assert.equal(pcm.length, 16000);
  const wav = new DataView(encodeWav(pcm, 16000));
  assert.equal(String.fromCharCode(wav.getUint8(0), wav.getUint8(1), wav.getUint8(2), wav.getUint8(3)), 'RIFF');
  assert.equal(wav.getUint32(24, true), 16000);
  assert.equal(wav.byteLength, 44 + 16000 * 2);
});
