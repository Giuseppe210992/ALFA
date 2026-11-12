/**
 * Provider vocali cloud (compatibili OpenAI):
 *  - transcribe(): Whisper / gpt-4o-transcribe via /audio/transcriptions
 *  - synthesize(): voci neurali via /audio/speech (restituisce lo stream audio)
 */
import { config } from '../config.js';
import { HttpError, upstreamError } from '../lib/http.js';
import { baseLang, languageName, normalizeDetected } from '../lib/languages.js';

export const sttReady = () => Boolean(config.stt.apiKey);
export const ttsReady = () => Boolean(config.tts.apiKey);

// Frasi che Whisper "allucina" su silenzio o rumore di fondo: le scartiamo.
const HALLUCINATIONS = [
  /^(thank you|thanks)( (so much|very much))?( for watching)?[.!]*$/i,
  /^(please )?(like and )?subscribe[.!]*$/i,
  /sottotitoli (creati|a cura) /i,
  /^grazie( a tutti)?( per (la visione|l'attenzione))?[.!]*$/i,
  /amara\.org/i,
  /^you[.!]*$/i,
  /^\W*$/,
];

export function isHallucination(text) {
  const t = text.trim();
  return HALLUCINATIONS.some((re) => re.test(t));
}

/** Una singola chiamata a /audio/transcriptions. */
async function transcribeOnce({ audio, mime, language, prompt, signal }) {
  const { baseUrl, apiKey, model } = config.stt;
  const ext = mime.includes('wav') ? 'wav' : mime.includes('mp4') ? 'mp4' : mime.includes('ogg') ? 'ogg' : 'webm';

  const form = new FormData();
  form.append('file', new Blob([audio], { type: mime }), `speech.${ext}`);
  form.append('model', model);
  form.append('temperature', '0');
  // whisper-1 e i modelli Whisper di Groq restituiscono lingua e affidabilità
  // con verbose_json; gpt-4o-transcribe supporta solo json.
  const verbose = !/gpt-4o/i.test(model);
  form.append('response_format', verbose ? 'verbose_json' : 'json');
  if (language) form.append('language', language);
  if (prompt) form.append('prompt', prompt);

  const res = await fetch(`${baseUrl}/audio/transcriptions`, {
    method: 'POST',
    signal,
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
  });
  if (!res.ok) throw await upstreamError(res, 'STT');
  const json = await res.json();

  let text = String(json.text || '').trim();
  let score = null; // log-probabilità media: più è alta, più la trascrizione è affidabile
  if (Array.isArray(json.segments) && json.segments.length) {
    // Scarta i segmenti in cui Whisper è poco sicuro che ci sia parlato.
    const speech = json.segments.filter((s) => !(s.no_speech_prob > 0.6 && s.avg_logprob < -0.7));
    text = speech.map((s) => s.text).join('').trim();
    if (speech.length) score = speech.reduce((n, s) => n + (s.avg_logprob ?? -1), 0) / speech.length;
  }
  if (isHallucination(text)) text = '';
  return {
    text,
    language: language || normalizeDetected(json.language),
    score,
    duration: json.duration ?? null,
  };
}

// Sotto questa affidabilità media la trascrizione forzata è considerata rumore.
const MIN_FORCED_SCORE = -1.0;

/**
 * Trascrive un segmento audio restando DENTRO la coppia di lingue.
 *
 * Whisper rileva la lingua da solo, ma su frasi brevi o rumorose sbaglia
 * (es. "¿Qué?" in spagnolo o frasi in islandese mentre si parla italiano).
 * Se la lingua rilevata non è una delle due della conversazione, ritrascriviamo
 * forzando ciascuna delle due lingue e teniamo il risultato più affidabile;
 * se nessuno dei due è affidabile, il segmento è rumore e viene scartato.
 *
 * @param {Buffer} audio      contenuto del file (WAV 16 kHz mono dal client)
 * @param {string} mime       content-type
 * @param {string[]} langs    lingue della conversazione (es. ["en-US","it-IT"])
 */
export async function transcribe({ audio, mime, langs = [], signal }) {
  if (!sttReady()) throw new HttpError(503, 'STT cloud non configurato (STT_API_KEY mancante)');
  const codes = [...new Set(langs.map(baseLang))];

  if (codes.length === 1) return transcribeOnce({ audio, mime, language: codes[0], signal });

  const prompt = codes.length > 1
    ? `Conversation between ${codes.map(languageName).join(' and ')} speakers.`
    : '';
  const first = await transcribeOnce({ audio, mime, prompt, signal });
  if (!first.text || codes.length < 2 || codes.includes(first.language)) return first;

  // Lingua fuori coppia: secondo passaggio forzato sulle due lingue.
  const forced = await Promise.all(
    codes.map((language) => transcribeOnce({ audio, mime, language, signal }).catch(() => null)),
  );
  const best = forced
    .filter((r) => r && r.text && r.score !== null)
    .sort((a, b) => b.score - a.score)[0];
  if (!best || best.score < MIN_FORCED_SCORE) {
    return { text: '', language: '', score: null, duration: first.duration, discarded: first.text };
  }
  return best;
}

/**
 * Sintesi vocale neurale: restituisce la Response fetch (body = stream mp3),
 * così il server può inoltrarla al client senza bufferizzarla.
 */
export async function synthesize({ text, lang, voice, signal }) {
  if (!ttsReady()) throw new HttpError(503, 'TTS cloud non configurato (TTS_API_KEY mancante)');
  const { baseUrl, apiKey, model } = config.tts;
  const body = {
    model,
    voice: voice || config.tts.voice,
    input: text,
    response_format: 'mp3',
  };
  // I modelli gpt-4o-*-tts accettano istruzioni di stile: voce naturale e colloquiale.
  if (/gpt-4o/i.test(model)) {
    body.instructions = `Speak in natural, fluent ${languageName(lang)} with a native accent, a warm conversational tone and a lively pace, like a human interpreter.`;
  }
  const res = await fetch(`${baseUrl}/audio/speech`, {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await upstreamError(res, 'TTS');
  return res;
}
