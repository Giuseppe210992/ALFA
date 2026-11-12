/**
 * Utility testuali pure (nessuna API del browser): testabili con `node --test`.
 */

/** Divide in parole ignorando spazi multipli. */
export const splitWords = (text) => String(text || '').trim().split(/\s+/).filter(Boolean);

/** Normalizza per confronti: minuscolo, senza punteggiatura né accenti. */
export function normalizeText(text) {
  return String(text || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Somiglianza di Jaccard tra gli insiemi di parole (0..1). */
export function similarity(a, b) {
  const A = new Set(normalizeText(a).split(' ').filter(Boolean));
  const B = new Set(normalizeText(b).split(' ').filter(Boolean));
  if (!A.size || !B.size) return 0;
  let inter = 0;
  for (const w of A) if (B.has(w)) inter++;
  return inter / (A.size + B.size - inter);
}

/**
 * Splitter di frasi per lo streaming della traduzione → TTS.
 * Emette una frase non appena è completa, così la sintesi vocale può partire
 * mentre l'LLM sta ancora generando il resto (latenza percepita molto più bassa).
 *
 * @param {(sentence: string) => void} onSentence
 * @param {{ softMax?: number, hardMax?: number }} opts
 *   softMax: oltre questa lunghezza si taglia anche su virgole/due punti
 *   hardMax: oltre questa lunghezza si taglia sull'ultimo spazio
 */
export function createSentenceSplitter(onSentence, { softMax = 90, hardMax = 180 } = {}) {
  let buffer = '';
  const END = /[.!?…。！？;]+["'»”)\]]*(\s+|$)/g;

  const emit = (s) => {
    const t = s.trim();
    // Ignora frammenti composti solo da punteggiatura
    if (/[\p{L}\p{N}]/u.test(t)) onSentence(t);
  };

  function drain(final) {
    // 1) Frasi complete (terminatore seguito da spazio)
    END.lastIndex = 0;
    let match;
    let cut = 0;
    while ((match = END.exec(buffer))) {
      const end = match.index + match[0].length;
      // A fine stream va bene anche il terminatore in coda; altrimenti
      // serve lo spazio successivo (evita di spezzare "3.5" o "e.g.").
      if (!final && end === buffer.length && !/\s$/.test(match[0])) break;
      emit(buffer.slice(cut, end));
      cut = end;
    }
    buffer = buffer.slice(cut);

    // 2) Frasi lunghe senza punto: taglio morbido su , : – per non attendere troppo
    while (buffer.length > softMax) {
      const window = buffer.slice(0, hardMax);
      let idx = Math.max(window.lastIndexOf(', '), window.lastIndexOf(': '), window.lastIndexOf(' – '));
      if (idx < 30) {
        if (buffer.length < hardMax) break;
        idx = window.lastIndexOf(' ');
        if (idx < 30) idx = hardMax;
      } else {
        idx += 1; // includi la virgola
      }
      emit(buffer.slice(0, idx));
      buffer = buffer.slice(idx);
    }

    if (final) {
      emit(buffer);
      buffer = '';
    }
  }

  return {
    push(text) {
      buffer += text;
      drain(false);
    },
    flush() {
      drain(true);
    },
  };
}
