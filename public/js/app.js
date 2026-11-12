/**
 * Entry point del frontend: collega impostazioni, UI, conversazione e PWA.
 */
import { getServerConfig, setAccessToken } from './api.js';
import { Conversation } from './conversation.js';
import { loadSettings, saveSettings, isIOS } from './settings.js';
import { listVoices, voicesReady, Speaker } from './tts.js';
import { baseLang, languageLabel } from './languages.js';
import { keepAwake } from './wakelock.js';
import * as ui from './ui.js';

const { el } = ui;
const SAMPLE = {
  it: 'Ciao! Questa è la voce che userò per tradurre.',
  en: 'Hi! This is the voice I will use to translate.',
  es: '¡Hola! Esta es la voz que usaré para traducir.',
  fr: 'Bonjour ! Voici la voix que j’utiliserai pour traduire.',
  de: 'Hallo! Das ist die Stimme, mit der ich übersetze.',
  pt: 'Olá! Esta é a voz que vou usar para traduzir.',
};

let serverConfig = null;
let settings = null;
let conversation = null;

// ─────────────────────────────────────────────────────────────────────────────
// Avvio
// ─────────────────────────────────────────────────────────────────────────────

async function init() {
  serverConfig = await getServerConfig();
  settings = loadSettings(serverConfig);
  setAccessToken(settings.accessToken);
  applyView();

  ui.fillLanguageSelect(el.myLang, settings.myLang);
  ui.fillLanguageSelect(el.theirLang, settings.theirLang);
  ui.setPhase('idle');

  if (!serverConfig) ui.toast('Server non raggiungibile: la traduzione non sarà disponibile finché sei offline.', { error: true, ms: 0 });
  else if (!serverConfig.translate.ready) ui.toast(`Il provider di traduzione "${serverConfig.translate.provider}" non è configurato sul server.`, { error: true, ms: 0 });

  bindEvents();
  registerServiceWorker();
  setupInstallPrompt();
  voicesReady(); // pre-carica l'elenco delle voci
}

function applyView() {
  document.body.classList.toggle('hide-source', !settings.showSource);
}

// ─────────────────────────────────────────────────────────────────────────────
// Conversazione
// ─────────────────────────────────────────────────────────────────────────────

async function startConversation() {
  conversation = new Conversation(settings, {
    onPhase: ui.setPhase,
    onListening: ui.setListening,
    onInterim: ui.setInterim,
    onLevel: ui.setLevel,
    onTurn: ui.renderTurn,
    onError: (msg, fatal) => ui.toast(msg, { error: fatal }),
  });
  try {
    await conversation.start();
    keepAwake(true);
  } catch (err) {
    conversation.stop();
    conversation = null;
    ui.toast(err.message || String(err), { error: true, ms: 8000 });
  }
}

function stopConversation() {
  conversation?.stop();
  conversation = null;
  keepAwake(false);
}

/** Applica nuove impostazioni riavviando la sessione se attiva (il tap è un gesto valido). */
async function restartIfRunning() {
  if (!conversation) return;
  stopConversation();
  await startConversation();
}

// ─────────────────────────────────────────────────────────────────────────────
// Eventi UI
// ─────────────────────────────────────────────────────────────────────────────

function bindEvents() {
  el.startBtn.addEventListener('click', () => {
    if (conversation) stopConversation();
    else startConversation();
  });

  const onLangChange = () => {
    if (el.myLang.value === el.theirLang.value) {
      ui.toast('Scegli due lingue diverse.');
      el.myLang.value = settings.myLang;
      el.theirLang.value = settings.theirLang;
      return;
    }
    settings.myLang = el.myLang.value;
    settings.theirLang = el.theirLang.value;
    saveSettings(settings);
    restartIfRunning();
  };
  el.myLang.addEventListener('change', onLangChange);
  el.theirLang.addEventListener('change', onLangChange);

  el.swapBtn.addEventListener('click', () => {
    [el.myLang.value, el.theirLang.value] = [el.theirLang.value, el.myLang.value];
    onLangChange();
  });

  // Tocco facoltativo per forzare la lingua ascoltata (solo modalità browser)
  el.listenChip.addEventListener('click', () => {
    const cur = el.listenChip.dataset.lang;
    if (!conversation || cur === 'auto') return;
    conversation.listenTo(baseLang(cur) === baseLang(settings.myLang) ? settings.theirLang : settings.myLang);
  });

  el.settingsBtn.addEventListener('click', openSettings);
  el.settings.addEventListener('close', onSettingsClosed);
  el.settingsForm.elements.rate.addEventListener('input', (e) => {
    document.getElementById('rateOut').textContent = `${Number(e.target.value).toFixed(2)}×`;
  });
  el.settingsForm.elements.ttsEngine.addEventListener('change', updateTtsFields);
  document.getElementById('testVoice').addEventListener('click', testVoices);
  document.getElementById('clearBtn').addEventListener('click', () => {
    ui.clearConversation();
    if (conversation) conversation.turns = [];
  });

  window.addEventListener('offline', () => ui.toast('Sei offline: la traduzione riprenderà quando torna la connessione.', { error: true, ms: 0 }));
  window.addEventListener('online', () => ui.toast('Di nuovo online.', { ms: 2500 }));

  // iOS sospende l'audio in background: al ritorno riattiviamo il contesto audio
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') conversation?.stt?.ctx?.resume?.().catch(() => {});
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Impostazioni
// ─────────────────────────────────────────────────────────────────────────────

function fillVoiceSelect(select, lang) {
  select.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = '';
  auto.textContent = 'Automatica (la più naturale)';
  select.append(auto);
  for (const v of listVoices(lang)) {
    const o = document.createElement('option');
    o.value = v.name;
    o.textContent = `${v.name}${v.localService ? '' : ' ☁︎'}`;
    select.append(o);
  }
  select.value = settings.voices[baseLang(lang)] || '';
}

function updateTtsFields() {
  const engine = el.settingsForm.elements.ttsEngine.value;
  el.settingsForm.querySelectorAll('[data-tts]').forEach((n) => { n.hidden = n.dataset.tts !== engine; });
}

async function openSettings() {
  await voicesReady();
  const f = el.settingsForm.elements;
  f.sttEngine.value = settings.sttEngine;
  f.segmentation.value = settings.segmentation;
  f.firstSpeaker.value = settings.firstSpeaker;
  f.autoTurn.checked = settings.autoTurn;
  f.fullDuplex.checked = settings.duplex === 'full';
  f.ttsEngine.value = settings.ttsEngine;
  f.cloudVoice.value = settings.cloudVoice;
  f.rate.value = settings.rate;
  f.showSource.checked = settings.showSource;
  f.accessToken.value = settings.accessToken;
  document.getElementById('rateOut').textContent = `${Number(settings.rate).toFixed(2)}×`;
  document.getElementById('myVoiceLabel').textContent = languageLabel(settings.myLang);
  document.getElementById('theirVoiceLabel').textContent = languageLabel(settings.theirLang);
  fillVoiceSelect(f.myVoice, settings.myLang);
  fillVoiceSelect(f.theirVoice, settings.theirLang);

  // Disabilita le opzioni cloud non configurate sul server
  f.sttEngine.querySelector('[value="cloud"]').disabled = !serverConfig?.stt?.ready;
  f.ttsEngine.querySelector('[value="cloud"]').disabled = !serverConfig?.tts?.ready;
  document.getElementById('tokenField').hidden = !serverConfig?.requiresToken && !settings.accessToken;
  document.getElementById('serverInfo').textContent = serverConfig
    ? `Server: traduzione ${serverConfig.translate.provider}${serverConfig.translate.ready ? '' : ' (non configurata)'} · STT cloud ${serverConfig.stt.ready ? serverConfig.stt.model : 'off'} · TTS cloud ${serverConfig.tts.ready ? 'on' : 'off'}${isIOS ? ' · iOS' : ''}`
    : 'Server non raggiungibile';
  updateTtsFields();
  el.settings.showModal();
}

function readForm() {
  const f = el.settingsForm.elements;
  return {
    ...settings,
    sttEngine: f.sttEngine.value,
    segmentation: f.segmentation.value,
    firstSpeaker: f.firstSpeaker.value,
    autoTurn: f.autoTurn.checked,
    duplex: f.fullDuplex.checked ? 'full' : 'half',
    ttsEngine: f.ttsEngine.value,
    cloudVoice: f.cloudVoice.value,
    rate: Number(f.rate.value),
    showSource: f.showSource.checked,
    accessToken: f.accessToken.value.trim(),
    voices: {
      ...settings.voices,
      [baseLang(settings.myLang)]: f.myVoice.value,
      [baseLang(settings.theirLang)]: f.theirVoice.value,
    },
  };
}

async function onSettingsClosed() {
  if (el.settings.returnValue !== 'save') return;
  settings = readForm();
  saveSettings(settings);
  setAccessToken(settings.accessToken);
  applyView();
  if (settings.accessToken) serverConfig = (await getServerConfig()) || serverConfig;
  restartIfRunning();
}

function testVoices() {
  const draft = readForm();
  const speaker = new Speaker({
    engine: draft.ttsEngine, rate: draft.rate, voices: draft.voices, cloudVoice: draft.cloudVoice,
  });
  speaker.unlock();
  for (const lang of [draft.theirLang, draft.myLang]) {
    speaker.speak(SAMPLE[baseLang(lang)] || SAMPLE.en, lang);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// PWA: service worker e installazione
// ─────────────────────────────────────────────────────────────────────────────

function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js').then((reg) => {
    // Nuova versione pronta: si attiva al prossimo avvio (non interrompiamo una conversazione)
    reg.addEventListener('updatefound', () => {
      const w = reg.installing;
      w?.addEventListener('statechange', () => {
        if (w.state === 'installed' && navigator.serviceWorker.controller && !conversation) {
          ui.toast('Aggiornamento disponibile: riapri l’app per usarlo.', { ms: 6000 });
        }
      });
    });
  }).catch((err) => console.warn('Service worker non registrato:', err));
}

function setupInstallPrompt() {
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  let dismissed = false;
  try { dismissed = localStorage.getItem('alfa.installDismissed') === '1'; } catch { /* */ }
  if (standalone || dismissed) return;

  el.installClose.addEventListener('click', () => {
    el.installBanner.hidden = true;
    try { localStorage.setItem('alfa.installDismissed', '1'); } catch { /* */ }
  });

  if (isIOS) {
    // Safari iOS non supporta beforeinstallprompt: mostriamo le istruzioni
    el.installText.textContent = 'Per installare: tocca Condividi ⬆︎ e poi “Aggiungi alla schermata Home”.';
    el.installBtn.hidden = true;
    el.installBanner.hidden = false;
    return;
  }

  let deferred = null;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferred = e;
    el.installBanner.hidden = false;
  });
  el.installBtn.addEventListener('click', async () => {
    if (!deferred) return;
    deferred.prompt();
    await deferred.userChoice;
    deferred = null;
    el.installBanner.hidden = true;
  });
  window.addEventListener('appinstalled', () => { el.installBanner.hidden = true; });
}

init();
