/**
 * Rendering dell'interfaccia: bolle della conversazione, stato, avvisi.
 * Nessuna logica di business: riceve dati e aggiorna il DOM.
 */
import { LANGUAGES, languageLabel, findLanguage } from './languages.js';

const $ = (id) => document.getElementById(id);

export const el = {
  conversation: $('conversation'),
  emptyState: $('emptyState'),
  startBtn: $('startBtn'),
  phaseText: $('phaseText'),
  interim: $('interim'),
  listenChip: $('listenChip'),
  toast: $('toast'),
  myLang: $('myLang'),
  theirLang: $('theirLang'),
  swapBtn: $('swapBtn'),
  settingsBtn: $('settingsBtn'),
  settings: $('settings'),
  settingsForm: $('settingsForm'),
  installBanner: $('installBanner'),
  installBtn: $('installBtn'),
  installClose: $('installClose'),
  installText: $('installText'),
};

const PHASES = {
  idle: { text: 'Pronto', label: 'Avvia' },
  listening: { text: 'In ascolto', label: 'Stop' },
  hearing: { text: 'Sento parlare…', label: 'Stop' },
  translating: { text: 'Traduco…', label: 'Stop' },
  speaking: { text: 'Parlo…', label: 'Stop' },
};

export function fillLanguageSelect(select, value) {
  select.innerHTML = '';
  for (const l of LANGUAGES) {
    const opt = document.createElement('option');
    opt.value = l.tag;
    opt.textContent = `${l.flag} ${l.name}`;
    select.append(opt);
  }
  select.value = findLanguage(value)?.tag || value;
}

export function setPhase(phase) {
  const p = PHASES[phase] || PHASES.idle;
  el.startBtn.dataset.phase = phase;
  el.startBtn.querySelector('.orb-label').textContent = p.label;
  el.startBtn.setAttribute('aria-label', phase === 'idle' ? 'Avvia la conversazione' : 'Ferma la conversazione');
  el.phaseText.textContent = p.text;
  if (phase === 'idle') el.listenChip.hidden = true;
}

export function setListening(langOrAuto) {
  el.listenChip.hidden = false;
  el.listenChip.textContent = langOrAuto === 'auto' ? '🌐 Lingua automatica' : `🎙️ ${languageLabel(langOrAuto)}`;
  el.listenChip.dataset.lang = langOrAuto;
}

export function setInterim(text) {
  // Il carattere LRM evita che "direction: rtl" sposti la punteggiatura
  el.interim.textContent = text ? `‎${text}‎` : '';
}

export function setLevel(v) {
  el.startBtn.style.setProperty('--level', String(v || 0));
}

let toastTimer = null;
export function toast(message, { error = false, ms = 5000 } = {}) {
  el.toast.textContent = message;
  el.toast.classList.toggle('error', error);
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => { el.toast.hidden = true; }, ms);
}

/** Crea o aggiorna la bolla di un turno. */
export function renderTurn(turn) {
  el.emptyState.hidden = true;
  let node = el.conversation.querySelector(`[data-turn="${turn.id}"]`);
  if (!node) {
    node = document.createElement('article');
    node.dataset.turn = turn.id;
    node.innerHTML = '<div class="meta"></div><div class="translation"></div><div class="source"></div>';
    el.conversation.append(node);
  }
  node.className = `turn ${turn.who} ${turn.state}`;
  node.querySelector('.meta').textContent = `${languageLabel(turn.from)} → ${languageLabel(turn.to)}`;
  node.querySelector('.translation').textContent = turn.state === 'error'
    ? `⚠️ ${turn.error || 'Errore'}`
    : turn.translation;
  node.querySelector('.source').textContent = turn.source;
  node.lang = turn.to;

  // Autoscroll solo se l'utente è già in fondo
  const c = el.conversation;
  if (c.scrollHeight - c.scrollTop - c.clientHeight < 160) c.scrollTop = c.scrollHeight;
}

export function clearConversation() {
  el.conversation.querySelectorAll('.turn').forEach((n) => n.remove());
  el.emptyState.hidden = false;
}
