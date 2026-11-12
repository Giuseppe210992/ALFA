import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createSegmenter } from '../public/js/stt/segmenter.js';

/** Timer finti controllabili dal test. */
function fakeTimers() {
  let pending = null;
  return {
    schedule: (fn) => { pending = fn; return 1; },
    cancel: () => { pending = null; },
    fire: () => { const f = pending; pending = null; f?.(); },
  };
}

function setup(opts = {}) {
  const segments = [];
  const t = fakeTimers();
  const seg = createSegmenter({
    onSegment: (s) => segments.push(s),
    schedule: t.schedule,
    cancel: t.cancel,
    ...opts,
  });
  return { seg, segments, t };
}

test('il risultato finale viene emesso intero', () => {
  const { seg, segments } = setup();
  seg.update([{ transcript: 'hello how are you', isFinal: true }]);
  assert.deepEqual(segments, [{ text: 'hello how are you', final: true }]);
});

test('parlato continuo: taglia in anticipo prima di una congiunzione', () => {
  const { seg, segments } = setup({ maxWords: 8, minWords: 3, tailGuard: 2 });
  const words = 'i went to the store this morning to buy bread and then i met my friend at the corner';
  seg.update([{ transcript: words, isFinal: false }]);
  assert.ok(segments.length >= 1, 'deve emettere almeno un segmento intermedio');
  assert.equal(segments[0].final, false);
  // Il primo taglio cade prima di "to buy"/"and" e non oltre maxWords+4 parole
  assert.ok(segments[0].text.split(' ').length <= 12);
  // Le ultime 2 parole (tailGuard) non vengono mai emesse in anticipo
  const emitted = segments.map((s) => s.text).join(' ');
  assert.ok(!emitted.endsWith('the corner'));
});

test('il finale emette solo la parte non ancora inviata', () => {
  const { seg, segments } = setup({ maxWords: 5, minWords: 2, tailGuard: 1 });
  seg.update([{ transcript: 'one two three four five six seven', isFinal: false }]);
  const before = segments.map((s) => s.text).join(' ');
  seg.update([{ transcript: 'one two three four five six seven eight', isFinal: true }]);
  const all = segments.map((s) => s.text).join(' ');
  assert.equal(all, 'one two three four five six seven eight');
  assert.ok(before.length > 0);
});

test('stabilità: testo intermedio fermo viene inviato dopo il timeout', () => {
  const { seg, segments, t } = setup();
  seg.update([{ transcript: 'see you tomorrow', isFinal: false }]);
  assert.equal(segments.length, 0);
  t.fire();
  assert.deepEqual(segments, [{ text: 'see you tomorrow', final: false }]);
  // Il finale successivo non duplica le parole
  seg.update([{ transcript: 'see you tomorrow', isFinal: true }]);
  assert.equal(segments.length, 1);
});

test('flush a fine sessione invia l’intermedio residuo', () => {
  const { seg, segments } = setup();
  seg.update([{ transcript: 'wait a second', isFinal: false }]);
  seg.flush();
  assert.deepEqual(segments, [{ text: 'wait a second', final: false }]);
});

test('deduplica i finali ripetuti (bug Android)', () => {
  const { seg, segments } = setup();
  seg.update([{ transcript: 'good morning', isFinal: true }]);
  seg.reset();
  seg.update([{ transcript: 'good morning', isFinal: true }]);
  assert.equal(segments.length, 1);
});
