/**
 * Impostazioni persistenti (localStorage, con fallback in memoria se bloccato).
 */
import { webSpeechSupported } from './stt/webspeech.js';
import { findLanguage } from './languages.js';

const KEY = 'alfa.settings.v1';

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

export const DEFAULTS = {
  myLang: 'it-IT',
  theirLang: 'en-US',
  /** 'webspeech' (gratis, ma su Android emette un beep a ogni riavvio) | 'cloud' (Whisper: silenzioso, lingua automatica) */
  sttEngine: 'webspeech',
  /** 'browser' (voci di sistema) | 'cloud' (voci neurali dal server) */
  ttsEngine: 'browser',
  /** 'half' (vivavoce: microfono in pausa mentre l'app parla) | 'full' (auricolari) */
  duplex: 'half',
  /** Alterna automaticamente la lingua ascoltata dopo ogni traduzione */
  autoTurn: true,
  /** Chi parla per primo (lingua iniziale del riconoscitore WebSpeech) */
  firstSpeaker: 'them',
  /** 'fast' | 'balanced' | 'accurate' */
  segmentation: 'balanced',
  rate: 1.05,
  /** voce preferita per lingua base, es. { it: 'Google italiano' } */
  voices: {},
  cloudVoice: '',
  accessToken: '',
  /** Mostra il testo originale sotto la traduzione */
  showSource: true,
};

export function loadSettings(serverConfig) {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(KEY) || 'null');
  } catch { /* storage non disponibile */ }

  const s = { ...DEFAULTS, ...(saved || {}) };

  // Primo avvio: scegli i motori migliori per questo dispositivo
  if (!saved) {
    const cloudStt = serverConfig?.stt?.ready;
    // Whisper è preferito ovunque sia disponibile: niente "beep" di sistema di
    // Android a ogni riavvio del riconoscitore e lingua rilevata in automatico.
    if (cloudStt) s.sttEngine = 'cloud';
    if (serverConfig?.tts?.ready && isIOS) s.ttsEngine = 'cloud';
    const nav = (navigator.language || 'it-IT');
    if (!nav.toLowerCase().startsWith('it')) {
      s.myLang = findLanguage(nav)?.tag || 'en-US';
      s.theirLang = 'it-IT';
    }
  }
  // Se il server non offre più STT cloud, torna a WebSpeech
  if (s.sttEngine === 'cloud' && serverConfig && !serverConfig.stt?.ready) s.sttEngine = 'webspeech';
  if (s.ttsEngine === 'cloud' && serverConfig && !serverConfig.tts?.ready) s.ttsEngine = 'browser';
  return s;
}

export function saveSettings(s) {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch { /* ignora: le impostazioni valgono per la sessione */ }
}

export { isIOS };
