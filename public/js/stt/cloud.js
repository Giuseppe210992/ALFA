/**
 * Adapter STT "cloud": microfono → VAD locale → segmenti WAV → Whisper (backend).
 *
 * Vantaggi rispetto a Web Speech API:
 *  - funziona su iPhone anche come PWA installata (dove SpeechRecognition è
 *    assente o instabile);
 *  - rileva AUTOMATICAMENTE la lingua di ogni frase → bidirezionale vero,
 *    senza dover indovinare chi sta parlando.
 *
 * Gestione del parlato continuo senza pause:
 *  - un segmento si chiude dopo ~550 ms di silenzio (hangover del VAD);
 *  - se la persona non si ferma mai, a `maxSegmentMs` si cerca il punto di
 *    energia minima negli ultimi 1.5 s (tipicamente uno spazio tra parole) e
 *    si taglia lì: la parte restante diventa l'inizio del segmento successivo,
 *    quindi nessuna sillaba viene persa o spezzata a metà.
 *  - le trascrizioni arrivano in ordine grazie a un numero di sequenza.
 */
import { Vad, rms, downsample, encodeWav, concat } from './audio-utils.js';
import { transcribe } from '../api.js';
import { baseLang } from '../languages.js';

const PREROLL_MS = 350;
const SEARCH_WINDOW_MS = 1500;
const MIN_SPEECH_MS = 400;

export const cloudSttSupported = () =>
  Boolean(navigator.mediaDevices?.getUserMedia && (globalThis.AudioContext || globalThis.webkitAudioContext));

export class CloudSTT {
  /**
   * @param {object} o
   * @param {string[]} o.langs          le due lingue della conversazione
   * @param {(s:{text, lang, final}) => void} o.onSegment
   * @param {(ev:{type, detail}) => void} o.onEvent      speechstart / speechend / level / error
   * @param {number} [o.maxSegmentMs]
   */
  constructor({ langs, onSegment, onEvent, maxSegmentMs = 7000, hangoverMs = 550 }) {
    this.langs = langs;
    this.onSegment = onSegment;
    this.onEvent = onEvent || (() => {});
    this.maxSegmentMs = maxSegmentMs;
    this.vad = new Vad({ hangoverMs });
    this.active = false;
    this.paused = false;
    this.blocks = []; // { samples, level } del segmento corrente
    this.preroll = [];
    this.seq = 0;
    this.nextToDeliver = 0;
    this.ready = new Map(); // seq → risultato in attesa di consegna ordinata
    this.inflight = new Set();
  }

  setLangs(langs) { this.langs = langs; }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
    this.ctx = new Ctx();
    await this.ctx.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url));
    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, 'pcm-capture');
    this.node.port.onmessage = (e) => this.#onBlock(e.data);
    this.source.connect(this.node);
    // Il worklet deve essere collegato a una destinazione per essere eseguito su
    // alcuni browser: usiamo un gain a zero per non riprodurre il microfono.
    this.sink = this.ctx.createGain();
    this.sink.gain.value = 0;
    this.node.connect(this.sink).connect(this.ctx.destination);
    if (this.ctx.state === 'suspended') await this.ctx.resume();
    this.active = true;
    this.paused = false;
    this.onEvent({ type: 'listening', detail: 'auto' });
  }

  stop() {
    this.active = false;
    this.#finishSegment();
    this.node?.disconnect();
    this.source?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close().catch(() => {});
    for (const c of this.inflight) c.abort();
    this.inflight.clear();
    this.node = this.source = this.stream = this.ctx = null;
  }

  /** Durante la riproduzione della traduzione ignoriamo il microfono. */
  pause() {
    if (!this.active || this.paused) return;
    this.#finishSegment(); // chi stava parlando: inviamo quanto già detto
    this.paused = true;
    this.vad.reset();
    this.preroll = [];
  }

  resume() {
    if (!this.active || !this.paused) return;
    if (this.ctx?.state === 'suspended') this.ctx.resume().catch(() => {});
    this.paused = false;
  }

  #onBlock(samples) {
    if (!this.active || this.paused) return;
    const ms = (samples.length / this.ctx.sampleRate) * 1000;
    const level = rms(samples);
    this.onEvent({ type: 'level', detail: Math.min(1, level / (this.vad.threshold * 3)) });

    const ev = this.vad.feed(level, ms);
    const block = { samples, level, ms };

    if (!this.vad.speaking && ev !== 'end') {
      // Fuori dal parlato: manteniamo un breve pre-roll per non tagliare l'attacco
      this.preroll.push(block);
      let total = this.preroll.reduce((n, b) => n + b.ms, 0);
      while (total > PREROLL_MS) total -= this.preroll.shift().ms;
      return;
    }

    if (ev === 'start') {
      this.blocks = [...this.preroll];
      this.preroll = [];
      this.onEvent({ type: 'speechstart' });
    }
    this.blocks.push(block);

    if (ev === 'end') {
      this.#finishSegment();
      this.onEvent({ type: 'speechend' });
      return;
    }

    // Parlato continuo: taglio intelligente sul punto di energia minima
    const duration = this.blocks.reduce((n, b) => n + b.ms, 0);
    if (duration >= this.maxSegmentMs) {
      let acc = 0;
      let best = this.blocks.length - 1;
      for (let i = this.blocks.length - 1; i > 0 && acc < SEARCH_WINDOW_MS; i--) {
        acc += this.blocks[i].ms;
        if (this.blocks[i].level < this.blocks[best].level) best = i;
      }
      const head = this.blocks.slice(0, best + 1);
      this.blocks = this.blocks.slice(best + 1);
      this.#sendSegment(head, false);
    }
  }

  #finishSegment() {
    if (!this.blocks.length) return;
    const blocks = this.blocks;
    this.blocks = [];
    this.#sendSegment(blocks, true);
  }

  async #sendSegment(blocks, final) {
    const voiced = blocks.filter((b) => b.level > this.vad.threshold).reduce((n, b) => n + b.ms, 0);
    if (voiced < MIN_SPEECH_MS || !this.ctx) return; // tosse, click, rumore breve

    const seq = this.seq++;
    const pcm = downsample(concat(blocks.map((b) => b.samples)), this.ctx.sampleRate, 16000);
    const wav = encodeWav(pcm, 16000);
    const ctrl = new AbortController();
    this.inflight.add(ctrl);
    this.onEvent({ type: 'transcribing', detail: true });
    try {
      const res = await transcribe(wav, this.langs, ctrl.signal);
      this.ready.set(seq, { ...res, final });
    } catch (err) {
      this.ready.set(seq, null);
      if (err.name !== 'AbortError') this.onEvent({ type: 'error', detail: { fatal: false, message: err.message } });
    } finally {
      this.inflight.delete(ctrl);
      this.onEvent({ type: 'transcribing', detail: this.inflight.size > 0 });
    }
    // Consegna in ordine di sequenza
    while (this.ready.has(this.nextToDeliver)) {
      const r = this.ready.get(this.nextToDeliver);
      this.ready.delete(this.nextToDeliver);
      this.nextToDeliver++;
      if (r?.text) {
        // Associa la lingua rilevata a una delle due della coppia
        const lang = this.langs.find((l) => baseLang(l) === r.language) || '';
        this.onSegment({ text: r.text, lang, detected: r.language, final: r.final });
      }
    }
  }
}
