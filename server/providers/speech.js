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

/**
 * Trascrive un segmento audio.
 * @param {Buffer} audio      contenuto del file (WAV 16 kHz mono dal client)
 * @param {string} mime       content-type
 * @param {string[]} langs    lingue candidate (es. ["en-US","it-IT"]); con una sola lingua la forziamo
 */
export async function transcribe({ audio, mime, langs = [], signal }) {
  if (!sttReady()) throw new HttpError(503, 'STT cloud non configurato (STT_API_KEY mancante)');
  const { baseUrl, apiKey, model } = config.stt;
  const ext = mime.includes('wav') ? 'wav' : mime.includes('mp4') ? 'mp4' : mime.includes('ogg') ? 'ogg' : 'webm';

  const form = new FormData();
  form.append('file', new Blob([audio], { type: mime }), `speech.${ext}`);
  form.append('model', model);
  form.append('temperature', '0');
  // whisper-1 e i modelli Whisper di Groq restituiscono la lingua con verbose_json;
  // gpt-4o-transcribe supporta solo json (in quel caso la lingua non è rilevata).
  const verbose = !/gpt-4o/i.test(model);
  form.append('response_format', verbose ? 'verbose_json' : 'json');
  if (langs.length === 1) form.append('language', baseLang(langs[0]));
  if (langs.length > 1) {
    // Un breve prompt con i nomi delle lingue aiuta il modello a restare tra le due.
    form.append('prompt', `Conversation between ${langs.map(languageName).join(' and ')} speakers.`);
  }

  const res = await fetch(`${baseUrl}/audio/transcriptions`, {
    method: 'POST',
    signal,
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
  });
  if (!res.ok) throw await upstreamError(res, 'STT');
  const json = await res.json();
  let text = String(json.text || '').trim();

  // Scarta i segmenti in cui Whisper è poco sicuro che ci sia parlato.
  if (Array.isArray(json.segments) && json.segments.length) {
    const speech = json.segments.filter((s) => !(s.no_speech_prob > 0.75 && s.avg_logprob < -0.8));
    text = speech.map((s) => s.text).join('').trim();
  }
  if (isHallucination(text)) text = '';

  return {
    text,
    language: normalizeDetected(json.language) || (langs.length === 1 ? baseLang(langs[0]) : ''),
    duration: json.duration ?? null,
  };
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
