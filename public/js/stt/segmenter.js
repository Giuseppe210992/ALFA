/**
 * Segmentatore intelligente per lo STT in streaming.
 *
 * Problema: SpeechRecognition produce un risultato "finale" solo dopo una
 * pausa. Se l'interlocutore parla di continuo (tipico dell'inglese parlato
 * veloce) la traduzione arriverebbe dopo 20-30 secondi.
 *
 * Soluzione: osserviamo i risultati *intermedi* e "committiamo" in anticipo
 * le parti già stabili, così la traduzione parte mentre la persona parla:
 *
 *  1. Taglio per lunghezza: quando il testo stabile non ancora inviato supera
 *     `maxWords`, si taglia sul confine più naturale (prima di una congiunzione
 *     o dopo una virgola) e si invia quel pezzo.
 *  2. Taglio per stabilità: se il testo intermedio non cambia per `stableMs`
 *     (micro-pausa troppo breve per il motore STT) lo si invia comunque.
 *  3. Le ultime `tailGuard` parole intermedie non vengono mai inviate in
 *     anticipo, perché il riconoscitore tende a correggerle.
 *  4. Quando arriva il risultato finale si invia solo la parte residua,
 *     tenendo traccia di quante parole di ogni risultato sono già state inviate.
 *
 * Il modulo è puro (timer iniettabili) e testato in tests/segmenter.test.js.
 */
import { splitWords, normalizeText } from '../text-utils.js';

// Parole prima delle quali è naturale spezzare una frase (più lingue).
const BREAK_BEFORE = new Set([
  // en
  'and', 'but', 'so', 'because', 'then', 'or', 'which', 'when', 'while', 'although', 'if', 'that',
  // it
  'e', 'ma', 'quindi', 'perché', 'perche', 'poi', 'oppure', 'che', 'quando', 'mentre', 'però', 'pero', 'se', 'allora',
  // es / pt / fr / de
  'y', 'pero', 'porque', 'entonces', 'mas', 'porque', 'então', 'et', 'mais', 'donc', 'parce', 'quand',
  'und', 'aber', 'weil', 'dann', 'oder', 'wenn',
]);

export function createSegmenter({
  onSegment,
  onInterim = () => {},
  minWords = 4,
  maxWords = 14,
  stableMs = 900,
  tailGuard = 2,
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (id) => clearTimeout(id),
} = {}) {
  /** indice risultato → numero di parole già inviate */
  let committed = new Map();
  let lastResults = [];
  let timer = null;
  let lastFinal = { text: '', at: 0 };

  function emit(text, final) {
    const t = text.trim();
    if (!t) return;
    // Alcuni browser Android ripetono il risultato finale: deduplica.
    const norm = normalizeText(t);
    if (final && norm === lastFinal.text && Date.now() - lastFinal.at < 3000) return;
    if (final) lastFinal = { text: norm, at: Date.now() };
    onSegment({ text: t, final });
  }

  /** Trova il punto di taglio ideale tra start (incluso) ed end (escluso). */
  function findCut(words, start, end) {
    for (let k = end - 1; k >= start + minWords; k--) {
      if (BREAK_BEFORE.has(words[k].toLowerCase().replace(/[^\p{L}]/gu, ''))) return k;
      if (/[,;:.!?]$/.test(words[k - 1])) return k;
    }
    return end;
  }

  function pendingInterim() {
    const parts = [];
    lastResults.forEach((r, i) => {
      if (r.isFinal) return;
      const words = splitWords(r.transcript);
      parts.push(words.slice(committed.get(i) || 0).join(' '));
    });
    return parts.join(' ').trim();
  }

  /** Commit forzato di tutto il testo intermedio pendente (stabilità o fine sessione). */
  function commitInterim(minimum) {
    let any = false;
    lastResults.forEach((r, i) => {
      if (r.isFinal) return;
      const words = splitWords(r.transcript);
      const c = committed.get(i) || 0;
      if (words.length - c >= minimum) {
        emit(words.slice(c).join(' '), false);
        committed.set(i, words.length);
        any = true;
      }
    });
    if (any) onInterim(pendingInterim());
  }

  function armStabilityTimer() {
    if (timer) cancel(timer);
    timer = null;
    const snapshot = pendingInterim();
    if (!snapshot) return;
    timer = schedule(() => {
      timer = null;
      if (pendingInterim() === snapshot) commitInterim(2);
    }, stableMs);
  }

  return {
    /**
     * @param {{transcript: string, isFinal: boolean}[]} results
     *   copia di event.results (tutti i risultati della sessione corrente)
     */
    update(results) {
      lastResults = results;
      results.forEach((r, i) => {
        const words = splitWords(r.transcript);
        const c = committed.get(i) || 0;
        if (r.isFinal) {
          if (words.length > c) emit(words.slice(c).join(' '), true);
          committed.set(i, Math.max(c, words.length));
          return;
        }
        // Risultato intermedio: taglio anticipato se troppo lungo
        let start = c;
        let stableEnd = words.length - tailGuard;
        while (stableEnd - start >= maxWords) {
          const cut = findCut(words, start, Math.min(stableEnd, start + maxWords + 4));
          emit(words.slice(start, cut).join(' '), false);
          start = cut;
          committed.set(i, cut);
          stableEnd = words.length - tailGuard;
        }
      });
      onInterim(pendingInterim());
      armStabilityTimer();
    },

    /** Fine sessione del riconoscitore: invia tutto ciò che resta. */
    flush() {
      if (timer) cancel(timer);
      timer = null;
      commitInterim(1);
      committed = new Map();
      lastResults = [];
      onInterim('');
    },

    /** Nuova sessione: gli indici dei risultati ripartono da zero. */
    reset() {
      if (timer) cancel(timer);
      timer = null;
      committed = new Map();
      lastResults = [];
      onInterim('');
    },
  };
}
