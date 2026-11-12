/**
 * Adapter STT basato su Web Speech API (SpeechRecognition).
 *
 * - Gratuito, latenza bassissima, ottimo su Chrome (Android/desktop) ed Edge.
 * - Il browser termina la sessione dopo silenzi o ~60 s: la riavviamo
 *   automaticamente (`onend` → restart) per un ascolto davvero continuo.
 * - Su Android `continuous=true` duplica i risultati: lì usiamo sessioni brevi
 *   (`continuous=false`) riavviate subito, con risultati intermedi attivi.
 * - Riconosce UNA lingua per volta: la conversazione decide quale ascoltare
 *   (vedi conversation.js → gestione dei turni).
 */
import { createSegmenter } from './segmenter.js';

const Recognition = globalThis.SpeechRecognition || globalThis.webkitSpeechRecognition;

export const webSpeechSupported = () => Boolean(Recognition);

export class WebSpeechSTT {
  /**
   * @param {object} o
   * @param {string} o.lang                 lingua iniziale (es. "en-US")
   * @param {(s:{text:string, lang:string, final:boolean}) => void} o.onSegment
   * @param {(text:string) => void} o.onInterim
   * @param {(ev:{type:string, detail?:any}) => void} o.onEvent   speechstart / error / state
   * @param {object} o.segmenter            opzioni del segmentatore
   */
  constructor({ lang, onSegment, onInterim, onEvent, segmenter = {} }) {
    this.lang = lang;
    this.onSegment = onSegment;
    this.onEvent = onEvent || (() => {});
    this.active = false;
    this.paused = false;
    this.rec = null;
    this.backoff = 0;
    this.restartTimer = null;
    this.isAndroid = /Android/i.test(navigator.userAgent);

    this.segmenter = createSegmenter({
      ...segmenter,
      onSegment: ({ text, final }) => this.onSegment({ text, final, lang: this.lang }),
      onInterim: onInterim || (() => {}),
    });
  }

  start() {
    if (!Recognition) throw new Error('SpeechRecognition non supportato da questo browser');
    this.active = true;
    this.paused = false;
    this.#startSession();
  }

  stop() {
    this.active = false;
    clearTimeout(this.restartTimer);
    this.#endSession(true);
  }

  /** Sospende l'ascolto (es. mentre l'app parla, per non tradurre se stessa). */
  pause() {
    if (!this.active || this.paused) return;
    this.paused = true;
    clearTimeout(this.restartTimer);
    this.#endSession(true);
  }

  resume() {
    if (!this.active || !this.paused) return;
    this.paused = false;
    this.#startSession();
  }

  /** Cambia la lingua ascoltata: riavvia la sessione se in corso. */
  setLang(lang) {
    if (lang === this.lang) return;
    this.lang = lang;
    this.onEvent({ type: 'lang', detail: lang });
    if (this.active && !this.paused) {
      this.#endSession(true);
      this.#scheduleRestart(50);
    }
  }

  #startSession() {
    if (!this.active || this.paused || this.rec) return;
    const rec = new Recognition();
    rec.lang = this.lang;
    rec.interimResults = true;
    rec.continuous = !this.isAndroid;
    rec.maxAlternatives = 1;

    rec.onresult = (e) => {
      this.backoff = 0;
      // Copiamo i risultati in un array semplice per il segmentatore
      const results = [];
      for (let i = 0; i < e.results.length; i++) {
        results.push({ transcript: e.results[i][0].transcript, isFinal: e.results[i].isFinal });
      }
      this.segmenter.update(results);
    };
    rec.onspeechstart = () => this.onEvent({ type: 'speechstart' });
    rec.onspeechend = () => this.onEvent({ type: 'speechend' });
    rec.onerror = (e) => {
      // "no-speech" e "aborted" sono normali in ascolto continuo
      if (e.error === 'no-speech' || e.error === 'aborted') return;
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        this.active = false;
        this.onEvent({ type: 'error', detail: { fatal: true, message: 'Permesso microfono negato' } });
        return;
      }
      if (e.error === 'language-not-supported') {
        this.onEvent({ type: 'error', detail: { fatal: false, message: `Lingua ${this.lang} non supportata dal riconoscitore` } });
      }
      this.backoff = Math.min(5000, (this.backoff || 250) * 2);
      this.onEvent({ type: 'error', detail: { fatal: false, message: `STT: ${e.error}` } });
    };
    rec.onend = () => {
      if (this.rec !== rec) return; // sessione già sostituita
      this.rec = null;
      // Ciò che era ancora "intermedio" va inviato, altrimenti andrebbe perso.
      this.segmenter.flush();
      this.#scheduleRestart(this.backoff || 0);
    };

    this.rec = rec;
    this.segmenter.reset();
    try {
      rec.start();
      this.onEvent({ type: 'listening', detail: this.lang });
    } catch {
      // start() lancia InvalidStateError se chiamato troppo presto: riprova
      this.rec = null;
      this.#scheduleRestart(300);
    }
  }

  #scheduleRestart(ms) {
    clearTimeout(this.restartTimer);
    if (!this.active || this.paused) return;
    this.restartTimer = setTimeout(() => this.#startSession(), ms);
  }

  #endSession(flush) {
    const rec = this.rec;
    if (!rec) return;
    this.rec = null;
    if (flush) this.segmenter.flush();
    rec.onend = null;
    rec.onresult = null;
    rec.onerror = null;
    try { rec.abort(); } catch { /* già fermo */ }
  }
}
