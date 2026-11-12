/**
 * Nomi delle lingue (per i prompt dell'LLM) e normalizzazione dei codici
 * restituiti da Whisper (che può rispondere "english", "English" o "en").
 */

export const LANGUAGE_NAMES = {
  it: 'Italian', en: 'English', es: 'Spanish', fr: 'French', de: 'German',
  pt: 'Portuguese', nl: 'Dutch', ru: 'Russian', zh: 'Chinese (Simplified)',
  ja: 'Japanese', ko: 'Korean', ar: 'Arabic', hi: 'Hindi', tr: 'Turkish',
  pl: 'Polish', ro: 'Romanian', uk: 'Ukrainian', el: 'Greek', sv: 'Swedish',
  da: 'Danish', no: 'Norwegian', fi: 'Finnish', cs: 'Czech', hu: 'Hungarian',
  he: 'Hebrew', th: 'Thai', vi: 'Vietnamese', id: 'Indonesian', sq: 'Albanian',
};

const NAME_TO_CODE = Object.fromEntries(
  Object.entries(LANGUAGE_NAMES).map(([code, name]) => [name.toLowerCase().split(' ')[0], code]),
);
// Varianti usate da Whisper
Object.assign(NAME_TO_CODE, { mandarin: 'zh', cantonese: 'zh', norwegian: 'no', 'nynorsk': 'no' });

/** "it-IT" → "it" */
export const baseLang = (tag = '') => String(tag).toLowerCase().split(/[-_]/)[0];

export const languageName = (tag) => LANGUAGE_NAMES[baseLang(tag)] || tag;

/** Normalizza la lingua rilevata da Whisper in un codice ISO-639-1. */
export function normalizeDetected(value) {
  if (!value) return '';
  const v = String(value).trim().toLowerCase();
  if (/^[a-z]{2}$/.test(v)) return v;
  return NAME_TO_CODE[v] || NAME_TO_CODE[v.split(/[\s(]/)[0]] || '';
}
