/**
 * Orchestratore della conversazione hands-free.
 *
 *   microfono ─► STT (WebSpeech | Cloud) ─► segmenti di testo
 *            ─► traduzione in streaming (LLM) ─► frasi
 *            ─► TTS in coda ordinata ─► altoparlante / auricolari
 *
 * Responsabilità principali:
 *  • Direzione della traduzione: chi sta parlando? (lingua rilevata da
 *    Whisper, oppure lingua attiva del riconoscitore + verifica dell'LLM).
 *  • Gestione dei turni (solo WebSpeech, che ascolta una lingua per volta):
 *    dopo aver letto una traduzione si ascolta la lingua di chi l'ha ricevuta,
 *    perché è lui che con ogni probabilità risponderà.
 *  • Half-duplex: mentre l'app parla il microfono è sospeso, così non
 *    ri-traduce la propria voce (disattivabile con gli auricolari).
 *  • Filtro anti-eco: scarta segmenti quasi identici a ciò che è appena stato
 *    pronunciato dall'app.
 *  • Ordine: le traduzioni partono in parallelo ma vengono lette nell'ordine
 *    in cui le frasi sono state pronunciate.
 */
import { WebSpeechSTT, webSpeechSupported } from './stt/webspeech.js';
import { CloudSTT } from './stt/cloud.js';
import { Speaker } from './tts.js';
import { translateStream } from './api.js';
import { baseLang } from './languages.js';
import { createSentenceSplitter, similarity } from './text-utils.js';

/** Preset del segmentatore: compromesso tra latenza e qualità della traduzione. */
export const SEGMENTATION = {
  fast: { maxWords: 9, stableMs: 650, minWords: 3 },
  balanced: { maxWords: 14, stableMs: 900, minWords: 4 },
  accurate: { maxWords: 22, stableMs: 1300, minWords: 6 },
};
const CLOUD_MAX_SEGMENT = { fast: 4500, balanced: 7000, accurate: 10000 };

const ECHO_WINDOW_MS = 12_000;
const TURN_SILENCE_MS = 12_000;

let turnCounter = 0;

export class Conversation {
  /**
   * @param {object} settings   vedi settings.js
   * @param {object} hooks      callback verso la UI
   *   onPhase(phase), onListening(langOrAuto), onInterim(text), onLevel(0..1),
   *   onTurn(turn), onError(message, fatal)
   */
  constructor(settings, hooks) {
    this.s = settings;
    this.h = hooks;
    this.turns = [];
    this.jobs = []; // coda di output ordinata
    this.pending = 0; // traduzioni in corso
    this.recentSpoken = [];
    this.running = false;
    this.hearing = false;
    this.lastSpokenTurn = null;
  }

  get myLang() { return this.s.myLang; }
  get theirLang() { return this.s.theirLang; }
  other(lang) { return baseLang(lang) === baseLang(this.myLang) ? this.theirLang : this.myLang; }
  who(lang) { return baseLang(lang) === baseLang(this.myLang) ? 'me' : 'them'; }

  // ───────────────────────────────────────────────────────────────────────────
  // Ciclo di vita
  // ───────────────────────────────────────────────────────────────────────────

  /** Deve essere chiamato dentro un gesto utente (sblocco audio iOS). */
  async start() {
    if (this.running) return;
    this.speaker = new Speaker({
      engine: this.s.ttsEngine,
      rate: this.s.rate,
      voices: this.s.voices,
      cloudVoice: this.s.cloudVoice,
      onStart: () => this.#onSpeakStart(),
      onIdle: () => this.#onSpeakIdle(),
    });
    this.speaker.unlock();

    const seg = SEGMENTATION[this.s.segmentation] || SEGMENTATION.balanced;
    const onEvent = (ev) => this.#onSttEvent(ev);
    const onSegment = (s) => this.#onSegment(s);

    if (this.s.sttEngine === 'cloud') {
      this.stt = new CloudSTT({
        langs: [this.theirLang, this.myLang],
        onSegment,
        onEvent,
        maxSegmentMs: CLOUD_MAX_SEGMENT[this.s.segmentation] || 7000,
      });
      await this.stt.start();
    } else {
      if (!webSpeechSupported()) throw new Error('Riconoscimento vocale del browser non disponibile: scegli "Cloud (Whisper)" nelle impostazioni.');
      // Su alcuni browser serve un getUserMedia esplicito per mostrare il prompt del permesso
      try {
        const s = await navigator.mediaDevices?.getUserMedia({ audio: true });
        s?.getTracks().forEach((t) => t.stop());
      } catch {
        throw new Error('Permesso microfono negato. Abilitalo nelle impostazioni del browser.');
      }
      this.stt = new WebSpeechSTT({
        lang: this.s.firstSpeaker === 'me' ? this.myLang : this.theirLang,
        onSegment,
        onInterim: (t) => this.h.onInterim(t),
        onEvent,
        segmenter: seg,
      });
      this.stt.start();
    }
    this.running = true;
    this.#armTurnTimer();
    this.#updatePhase();
  }

  stop() {
    this.running = false;
    clearTimeout(this.turnTimer);
    clearTimeout(this.resumeTimer);
    this.stt?.stop();
    this.speaker?.cancel();
    for (const j of this.jobs) j.ctrl?.abort();
    this.jobs = [];
    this.pending = 0;
    this.h.onInterim('');
    this.h.onLevel(0);
    this.h.onPhase('idle');
  }

  /** Forza la lingua ascoltata (tap sul chip della lingua): solo WebSpeech. */
  listenTo(lang) {
    if (this.stt instanceof WebSpeechSTT) this.stt.setLang(lang);
    this.#armTurnTimer();
  }

  // ───────────────────────────────────────────────────────────────────────────
  // STT
  // ───────────────────────────────────────────────────────────────────────────

  #onSttEvent({ type, detail }) {
    switch (type) {
      case 'listening':
      case 'lang':
        this.h.onListening(detail);
        break;
      case 'speechstart':
        this.hearing = true;
        this.#armTurnTimer();
        this.#updatePhase();
        break;
      case 'speechend':
        this.hearing = false;
        this.#updatePhase();
        break;
      case 'level':
        this.h.onLevel(detail);
        break;
      case 'transcribing':
        this.h.onInterim(detail ? '…' : '');
        break;
      case 'error':
        this.h.onError(detail.message, detail.fatal);
        if (detail.fatal) this.stop();
        break;
      default:
    }
  }

  #isEcho(text) {
    const now = Date.now();
    this.recentSpoken = this.recentSpoken.filter((r) => now - r.at < ECHO_WINDOW_MS);
    return this.recentSpoken.some((r) => similarity(r.text, text) >= 0.5);
  }

  #onSegment({ text, lang }) {
    if (!this.running || !text) return;
    this.hearing = false;
    this.#armTurnTimer();
    if (this.#isEcho(text)) return;

    // Lingua sorgente: rilevata (cloud) o del riconoscitore. Se Whisper ha
    // rilevato una terza lingua, ipotizziamo l'interlocutore e lasciamo
    // decidere all'LLM tramite il tag di rilevamento.
    const from = lang || (this.lastSpokenTurn ? this.lastSpokenTurn.to : this.theirLang);
    this.#translate(text, from);
  }

  // ───────────────────────────────────────────────────────────────────────────
  // Traduzione → coda di output
  // ───────────────────────────────────────────────────────────────────────────

  #translate(text, from) {
    const to = this.other(from);
    const turn = {
      id: ++turnCounter, from, to, who: this.who(from),
      source: text, translation: '', state: 'translating', at: Date.now(),
    };
    this.turns.push(turn);
    this.h.onTurn(turn);

    const job = { turn, sentences: [], done: false, ctrl: new AbortController() };
    this.jobs.push(job);
    this.pending++;
    this.#updatePhase();

    const splitter = createSentenceSplitter((sentence) => {
      job.sentences.push(sentence);
      this.#drain();
    });

    const context = this.turns
      .filter((t) => t.state === 'done' && t !== turn)
      .slice(-4)
      .map((t) => ({ source: t.source, translation: t.translation }));

    translateStream({
      text, from, to, context,
      detect: true,
      signal: job.ctrl.signal,
      onMeta: ({ detected }) => {
        if (detected === '??') {
          // Rumore o frase incomprensibile: niente da leggere
          turn.state = 'discarded';
          job.ctrl.abort();
          this.h.onTurn(turn);
          return;
        }
        if (detected && detected === baseLang(to)) {
          // Il riconoscitore ascoltava la lingua sbagliata: l'LLM ha già
          // invertito la direzione, aggiorniamo turno e lingua ascoltata.
          [turn.from, turn.to] = [turn.to, turn.from];
          turn.who = this.who(turn.from);
          this.h.onTurn(turn);
          this.listenTo(turn.from);
        }
      },
      onDelta: (t) => {
        turn.translation += t;
        this.h.onTurn(turn);
        splitter.push(t);
      },
    })
      .then(() => {
        splitter.flush();
        if (turn.state === 'translating') turn.state = 'done';
        turn.translation = turn.translation.trim();
      })
      .catch((err) => {
        if (turn.state === 'discarded' || !this.running) return;
        turn.state = 'error';
        turn.error = err.message;
        this.h.onError(`Traduzione non riuscita: ${err.message}`, false);
      })
      .finally(() => {
        job.done = true;
        this.pending--;
        this.h.onTurn(turn);
        this.#drain();
        this.#updatePhase();
      });
  }

  /** Invia al TTS le frasi pronte, rispettando l'ordine dei turni. */
  #drain() {
    while (this.jobs.length) {
      const job = this.jobs[0];
      if (job.turn.state === 'discarded') job.sentences = [];
      while (job.sentences.length) {
        const sentence = job.sentences.shift();
        this.recentSpoken.push({ text: sentence, at: Date.now() });
        this.lastSpokenTurn = job.turn;
        this.speaker.speak(sentence, job.turn.to);
      }
      if (!job.done) break;
      this.jobs.shift();
    }
  }

  // ───────────────────────────────────────────────────────────────────────────
  // Half-duplex e turni
  // ───────────────────────────────────────────────────────────────────────────

  #onSpeakStart() {
    clearTimeout(this.resumeTimer);
    if (this.s.duplex === 'half') this.stt?.pause();
    this.#updatePhase();
  }

  #onSpeakIdle() {
    if (!this.running) return;
    // Dopo una traduzione è il destinatario che risponde: ascoltiamo la sua lingua.
    if (this.s.autoTurn && this.lastSpokenTurn && this.stt instanceof WebSpeechSTT) {
      this.stt.setLang(this.lastSpokenTurn.to);
    }
    clearTimeout(this.resumeTimer);
    // Piccolo ritardo: evita di catturare la coda del suono dall'altoparlante
    this.resumeTimer = setTimeout(() => {
      this.stt?.resume();
      this.#armTurnTimer();
      this.#updatePhase();
    }, this.s.duplex === 'half' ? 350 : 0);
    this.#updatePhase();
  }

  /**
   * Fallback dei turni per WebSpeech: se nessuno parla per un po' nella lingua
   * attesa, alterna la lingua ascoltata (l'altra persona potrebbe voler parlare).
   */
  #armTurnTimer() {
    clearTimeout(this.turnTimer);
    if (!this.running || !this.s.autoTurn || !(this.stt instanceof WebSpeechSTT)) return;
    this.turnTimer = setTimeout(() => {
      if (!this.running || this.speaker.speaking) return this.#armTurnTimer();
      this.stt.setLang(this.other(this.stt.lang));
      this.#armTurnTimer();
    }, TURN_SILENCE_MS);
  }

  #updatePhase() {
    if (!this.running) return this.h.onPhase('idle');
    if (this.speaker?.speaking) return this.h.onPhase('speaking');
    if (this.hearing) return this.h.onPhase('hearing');
    if (this.pending > 0) return this.h.onPhase('translating');
    return this.h.onPhase('listening');
  }
}
