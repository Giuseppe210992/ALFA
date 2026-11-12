/**
 * Sintesi vocale con coda ordinata.
 *
 * Motori:
 *  - "browser": SpeechSynthesis con selezione automatica della voce più
 *    naturale disponibile (Neural/Natural/Online di Edge, Google di Chrome,
 *    Premium/Enhanced/Siri di iOS), evitando le voci "novelty" o compatte.
 *  - "cloud":   voci neurali dal backend (/api/tts). La frase successiva viene
 *    pre-caricata mentre la precedente è in riproduzione → nessuno stacco.
 *    In caso di errore si ripiega automaticamente sul motore del browser.
 *
 * Eventi: onStart() quando inizia a parlare, onIdle() quando la coda è vuota.
 * La conversazione li usa per sospendere il microfono (half-duplex).
 */
import { synthesize } from './api.js';
import { baseLang } from './languages.js';

const synth = globalThis.speechSynthesis;

// Voci giocattolo di macOS/iOS da non usare mai
const NOVELTY = /albert|bad news|bahh|bells|boing|bubbles|cellos|deranged|good news|hysterical|jester|organ|superstar|trinoids|whisper|wobble|zarvox|junior|ralph|fred|kathy|grandma|grandpa|eddy|flo|reed|rocko|sandy|shelley/i;

/** Punteggio di "naturalezza" di una voce per una lingua. Più alto = meglio. */
export function scoreVoice(voice, lang) {
  const vLang = (voice.lang || '').replace('_', '-').toLowerCase();
  const want = lang.toLowerCase();
  let score = 0;
  if (vLang === want) score += 100;
  else if (baseLang(vLang) === baseLang(want)) score += 60;
  else return -1;

  const name = voice.name || '';
  if (/neural|natural/i.test(name)) score += 60; // Edge / Azure
  if (/online/i.test(name)) score += 15;
  if (/premium/i.test(name)) score += 55; // iOS/macOS scaricate
  if (/enhanced|siri/i.test(name)) score += 45;
  if (/google/i.test(name)) score += 35; // Chrome (rete)
  if (/compact|espeak|pico/i.test(name)) score -= 40;
  if (NOVELTY.test(name)) score -= 200;
  if (voice.localService === false) score += 5; // spesso più naturali
  if (voice.default) score += 3;
  return score;
}

export function listVoices(lang) {
  if (!synth) return [];
  return synth.getVoices()
    .map((v) => ({ v, s: scoreVoice(v, lang) }))
    .filter((x) => x.s >= 0)
    .sort((a, b) => b.s - a.s)
    .map((x) => x.v);
}

/** Le voci si caricano in modo asincrono (Chrome/Android): attende il primo elenco. */
export function voicesReady(timeout = 2000) {
  if (!synth) return Promise.resolve([]);
  const now = synth.getVoices();
  if (now.length) return Promise.resolve(now);
  return new Promise((resolve) => {
    const done = () => resolve(synth.getVoices());
    synth.addEventListener('voiceschanged', done, { once: true });
    setTimeout(done, timeout);
  });
}

export class Speaker {
  /**
   * @param {object} o
   * @param {'browser'|'cloud'} o.engine
   * @param {number} o.rate                 velocità (0.8–1.3)
   * @param {Record<string,string>} o.voices  nome voce preferita per lingua (motore browser)
   * @param {string} o.cloudVoice           voce del motore cloud
   */
  constructor({ engine = 'browser', rate = 1.05, voices = {}, cloudVoice = '', onStart, onIdle } = {}) {
    Object.assign(this, { engine, rate, voices, cloudVoice });
    this.onStart = onStart || (() => {});
    this.onIdle = onIdle || (() => {});
    this.queue = []; // { text, lang, audio?: Promise<Blob> }
    this.busy = false;
    this.audio = new Audio();
    this.audio.preload = 'auto';
    this.keepAlive = null;
  }

  get speaking() { return this.busy; }

  /**
   * Va chiamato dentro un gesto dell'utente (tap su "Avvia"): iOS e Chrome
   * bloccano l'audio automatico finché la pagina non ha "sbloccato" i motori.
   */
  unlock() {
    try {
      if (synth) {
        const u = new SpeechSynthesisUtterance(' ');
        u.volume = 0;
        synth.speak(u);
      }
      // Clip WAV vuota per sbloccare l'elemento <audio>
      this.audio.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
      this.audio.play().catch(() => {});
    } catch { /* best effort */ }
  }

  speak(text, lang) {
    const item = { text, lang };
    if (this.engine === 'cloud') item.audio = this.#prefetch(item);
    this.queue.push(item);
    this.#pump();
  }

  /** Interrompe tutto (es. stop della sessione). */
  cancel() {
    this.queue = [];
    synth?.cancel();
    this.audio.pause();
    this.audio.removeAttribute('src');
    this.#setBusy(false);
  }

  #setBusy(b) {
    if (b === this.busy) return;
    this.busy = b;
    if (b) this.onStart();
    else this.onIdle();
  }

  #prefetch(item) {
    return synthesize(item.text, item.lang, this.cloudVoice).catch((err) => {
      console.warn('TTS cloud non disponibile, uso il browser:', err.message);
      return null;
    });
  }

  async #pump() {
    if (this.pumping) return;
    this.pumping = true;
    try {
      while (this.queue.length) {
        const item = this.queue.shift();
        this.#setBusy(true);
        let blob = null;
        if (item.audio) blob = await item.audio;
        if (blob) await this.#playBlob(blob);
        else await this.#speakBrowser(item.text, item.lang);
      }
    } finally {
      this.pumping = false;
      this.#setBusy(false);
    }
  }

  #playBlob(blob) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob);
      const done = () => {
        URL.revokeObjectURL(url);
        this.audio.onended = this.audio.onerror = null;
        resolve();
      };
      this.audio.onended = done;
      this.audio.onerror = done;
      this.audio.src = url;
      this.audio.playbackRate = this.rate;
      this.audio.play().catch(done);
    });
  }

  #speakBrowser(text, lang) {
    if (!synth) return Promise.resolve();
    return new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = lang;
      const preferred = this.voices[baseLang(lang)];
      const all = listVoices(lang);
      u.voice = all.find((v) => v.name === preferred) || all[0] || null;
      u.rate = this.rate;
      u.pitch = 1;
      u.volume = 1;

      // Bug di Chrome: le utterance lunghe si bloccano dopo ~15 s senza resume().
      clearInterval(this.keepAlive);
      this.keepAlive = setInterval(() => {
        if (synth.speaking && !synth.paused) { synth.pause(); synth.resume(); }
      }, 10_000);

      // Rete di sicurezza: alcuni browser non emettono mai "end"
      const maxMs = 4000 + text.length * 120;
      const guard = setTimeout(() => finish(), maxMs);
      const finish = () => {
        clearTimeout(guard);
        clearInterval(this.keepAlive);
        resolve();
      };
      u.onend = finish;
      u.onerror = finish;
      try {
        synth.speak(u);
      } catch {
        finish(); // mai bloccare la coda per un errore del motore
      }
    });
  }
}
