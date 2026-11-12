/**
 * Lingue supportate. Il tag BCP-47 completo (es. "en-US") serve a
 * SpeechRecognition e SpeechSynthesis; il codice base ("en") al backend.
 */
export const LANGUAGES = [
  { tag: 'it-IT', name: 'Italiano', flag: '🇮🇹' },
  { tag: 'en-US', name: 'English (US)', flag: '🇺🇸' },
  { tag: 'en-GB', name: 'English (UK)', flag: '🇬🇧' },
  { tag: 'es-ES', name: 'Español', flag: '🇪🇸' },
  { tag: 'fr-FR', name: 'Français', flag: '🇫🇷' },
  { tag: 'de-DE', name: 'Deutsch', flag: '🇩🇪' },
  { tag: 'pt-BR', name: 'Português (BR)', flag: '🇧🇷' },
  { tag: 'pt-PT', name: 'Português (PT)', flag: '🇵🇹' },
  { tag: 'nl-NL', name: 'Nederlands', flag: '🇳🇱' },
  { tag: 'pl-PL', name: 'Polski', flag: '🇵🇱' },
  { tag: 'ro-RO', name: 'Română', flag: '🇷🇴' },
  { tag: 'sq-AL', name: 'Shqip', flag: '🇦🇱' },
  { tag: 'el-GR', name: 'Ελληνικά', flag: '🇬🇷' },
  { tag: 'ru-RU', name: 'Русский', flag: '🇷🇺' },
  { tag: 'uk-UA', name: 'Українська', flag: '🇺🇦' },
  { tag: 'tr-TR', name: 'Türkçe', flag: '🇹🇷' },
  { tag: 'ar-SA', name: 'العربية', flag: '🇸🇦' },
  { tag: 'hi-IN', name: 'हिन्दी', flag: '🇮🇳' },
  { tag: 'zh-CN', name: '中文 (简体)', flag: '🇨🇳' },
  { tag: 'ja-JP', name: '日本語', flag: '🇯🇵' },
  { tag: 'ko-KR', name: '한국어', flag: '🇰🇷' },
  { tag: 'sv-SE', name: 'Svenska', flag: '🇸🇪' },
];

export const baseLang = (tag = '') => String(tag).toLowerCase().split(/[-_]/)[0];

export const findLanguage = (tag) =>
  LANGUAGES.find((l) => l.tag === tag) || LANGUAGES.find((l) => baseLang(l.tag) === baseLang(tag));

export const languageLabel = (tag) => {
  const l = findLanguage(tag);
  return l ? `${l.flag} ${l.name}` : tag;
};
