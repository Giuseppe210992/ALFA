/**
 * Provider di traduzione in streaming.
 *
 * Interfaccia comune:
 *   translateStream({ text, from, to, context, detect, signal, onMeta, onDelta })
 *     - onMeta({ detected })  → lingua effettiva del testo (solo provider LLM)
 *     - onDelta(textChunk)    → frammenti di traduzione, appena disponibili
 *
 * I provider LLM (OpenAI-compatibile, Anthropic) sono istruiti a far precedere
 * la traduzione da un tag `[[xx]]` con la lingua realmente parlata: serve al
 * client per capire se il riconoscimento vocale era impostato sulla lingua
 * sbagliata (es. l'interlocutore risponde in italiano mentre ascoltavamo in
 * inglese) e per invertire la direzione della traduzione al volo.
 */
import { config } from '../config.js';
import { HttpError, iterateSse, upstreamError } from '../lib/http.js';
import { baseLang, languageName } from '../lib/languages.js';

// ─────────────────────────────────────────────────────────────────────────────
// Prompt
// ─────────────────────────────────────────────────────────────────────────────

export function buildPrompt({ text, from, to, context = [], detect = true }) {
  const src = languageName(from);
  const tgt = languageName(to);
  const srcCode = baseLang(from);
  const tgtCode = baseLang(to);

  const rules = [
    `You are a professional real-time voice interpreter between ${src} and ${tgt}.`,
    'The user message contains a live speech-recognition transcript: it may lack punctuation and contain recognition errors.',
    `The transcript is expected to be in ${src}.`,
    '',
    'Rules:',
  ];
  if (detect) {
    rules.push(
      `1. First output a tag with the language the transcript is ACTUALLY in: [[${srcCode}]] if ${src}, [[${tgtCode}]] if it is actually ${tgt}, or [[??]] if it is unintelligible noise or not meaningful speech.`,
      `2. Right after the tag output ONLY the translation from ${src} into ${tgt}. If the transcript is actually in ${tgt}, translate it into ${src} instead. After [[??]] output nothing else.`,
    );
  } else {
    rules.push(`1. Output ONLY the translation from ${src} into ${tgt}.`);
  }
  rules.push(
    `${detect ? 3 : 2}. Silently fix obvious speech-recognition errors using the context. Preserve meaning, tone and register; use a natural spoken style with correct punctuation.`,
    `${detect ? 4 : 3}. You only translate: never answer questions, never add notes, quotes or explanations.`,
  );

  let user = '';
  const recent = context.slice(-4).filter((c) => c && c.source && c.translation);
  if (recent.length) {
    user += 'Recent conversation, for context only (do NOT translate it):\n';
    for (const c of recent) user += `- ${c.source} → ${c.translation}\n`;
    user += '\n';
  }
  user += `Transcript to translate:\n${text}`;

  return { system: rules.join('\n'), user };
}

/**
 * Estrae il tag iniziale `[[xx]]` da uno stream di testo.
 * Tutto ciò che segue il tag viene inoltrato a `onText`, senza spazi iniziali.
 */
export function createTagParser(onTag, onText) {
  let buffer = '';
  let tagDone = false;
  let stripLeading = true;

  const emit = (s) => {
    if (stripLeading) {
      s = s.replace(/^\s+/, '');
      if (!s) return;
      stripLeading = false;
    }
    onText(s);
  };

  return {
    push(chunk) {
      if (tagDone) return emit(chunk);
      buffer += chunk;
      const t = buffer.replace(/^\s+/, '');
      if (!t) return;
      if (t.startsWith('[[')) {
        const end = t.indexOf(']]');
        if (end !== -1) {
          tagDone = true;
          onTag(t.slice(2, end).trim().toLowerCase());
          emit(t.slice(end + 2));
        } else if (t.length > 16) {
          // Tag malformato: lo trattiamo come testo normale
          tagDone = true;
          emit(t);
        }
      } else if (!'[['.startsWith(t.slice(0, 2))) {
        // Nessun tag: il modello ha risposto direttamente
        tagDone = true;
        emit(t);
      }
    },
    flush() {
      if (!tagDone && buffer.trim()) {
        tagDone = true;
        emit(buffer);
      }
    },
  };
}

/**
 * Rimuove i blocchi di ragionamento <think>…</think> che alcuni modelli
 * (Qwen, DeepSeek, GLM… spesso scelti dai router come FreeLLMAPI con "auto")
 * inseriscono nel testo: altrimenti verrebbero letti ad alta voce.
 */
export function createThinkFilter(onText) {
  const OPEN = '<think>';
  const CLOSE = '</think>';
  let buf = '';
  let inThink = false;
  // Lunghezza del suffisso di `buf` che potrebbe essere l'inizio di `tag`
  const partial = (tag) => {
    for (let n = Math.min(tag.length - 1, buf.length); n > 0; n--) {
      if (tag.startsWith(buf.slice(-n))) return n;
    }
    return 0;
  };
  return {
    push(chunk) {
      buf += chunk;
      for (;;) {
        if (inThink) {
          const i = buf.indexOf(CLOSE);
          if (i === -1) { buf = buf.slice(buf.length - partial(CLOSE)); return; }
          buf = buf.slice(i + CLOSE.length);
          inThink = false;
        } else {
          const i = buf.indexOf(OPEN);
          if (i === -1) {
            const keep = partial(OPEN);
            if (buf.length > keep) onText(buf.slice(0, buf.length - keep));
            buf = buf.slice(buf.length - keep);
            return;
          }
          if (i > 0) onText(buf.slice(0, i));
          buf = buf.slice(i + OPEN.length);
          inThink = true;
        }
      }
    },
    flush() {
      if (!inThink && buf) onText(buf);
      buf = '';
    },
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Provider
// ─────────────────────────────────────────────────────────────────────────────

async function openaiStream(opts, prompt, onChunk) {
  const { baseUrl, apiKey, model } = config.translate.openai;
  const res = await fetch(`${baseUrl}/chat/completions`, {
    method: 'POST',
    signal: opts.signal,
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
    },
    body: JSON.stringify({
      model,
      stream: true,
      messages: [
        { role: 'system', content: prompt.system },
        { role: 'user', content: prompt.user },
      ],
    }),
  });
  if (!res.ok) throw await upstreamError(res, 'LLM');
  const think = createThinkFilter(onChunk);
  for await (const { data } of iterateSse(res)) {
    if (data === '[DONE]') break;
    let delta;
    try {
      delta = JSON.parse(data).choices?.[0]?.delta?.content;
    } catch { continue; /* riga keep-alive o non JSON */ }
    if (delta) think.push(delta);
  }
  think.flush();
}

async function anthropicStream(opts, prompt, onChunk) {
  const { apiKey, model } = config.translate.anthropic;
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    signal: opts.signal,
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model,
      max_tokens: 1024,
      stream: true,
      system: prompt.system,
      messages: [{ role: 'user', content: prompt.user }],
    }),
  });
  if (!res.ok) throw await upstreamError(res, 'Anthropic');
  for await (const { event, data } of iterateSse(res)) {
    if (event === 'message_stop') break;
    if (event === 'error') throw new HttpError(502, `Anthropic: ${data.slice(0, 200)}`);
    if (event !== 'content_block_delta') continue;
    try {
      const delta = JSON.parse(data).delta;
      if (delta?.type === 'text_delta' && delta.text) onChunk(delta.text);
    } catch { /* ignora */ }
  }
}

/** DeepL distingue alcune varianti regionali nella lingua di destinazione. */
function deeplTarget(tag) {
  const [base, region = ''] = String(tag).split(/[-_]/);
  const b = base.toUpperCase();
  if (b === 'EN') return region.toUpperCase() === 'GB' ? 'EN-GB' : 'EN-US';
  if (b === 'PT') return region.toUpperCase() === 'PT' ? 'PT-PT' : 'PT-BR';
  if (b === 'ZH') return 'ZH-HANS';
  return b;
}

async function deeplTranslate(opts) {
  const { apiKey, baseUrl } = config.translate.deepl;
  const res = await fetch(`${baseUrl}/v2/translate`, {
    method: 'POST',
    signal: opts.signal,
    headers: { 'Content-Type': 'application/json', Authorization: `DeepL-Auth-Key ${apiKey}` },
    body: JSON.stringify({
      text: [opts.text],
      source_lang: baseLang(opts.from).toUpperCase(),
      target_lang: deeplTarget(opts.to),
    }),
  });
  if (!res.ok) throw await upstreamError(res, 'DeepL');
  const json = await res.json();
  return json.translations?.[0]?.text || '';
}

/** MyMemory accetta max ~500 byte per richiesta: spezziamo per frasi. */
function chunkText(text, max = 450) {
  const parts = text.match(/[^.!?]+[.!?]*\s*/g) || [text];
  const out = [];
  let cur = '';
  for (const p of parts) {
    if ((cur + p).length > max && cur) { out.push(cur); cur = ''; }
    cur += p;
    while (cur.length > max) { out.push(cur.slice(0, max)); cur = cur.slice(max); }
  }
  if (cur.trim()) out.push(cur);
  return out;
}

async function mymemoryTranslate(opts, onChunk) {
  const pair = `${baseLang(opts.from)}|${baseLang(opts.to)}`;
  for (const piece of chunkText(opts.text)) {
    const url = new URL('https://api.mymemory.translated.net/get');
    url.searchParams.set('q', piece);
    url.searchParams.set('langpair', pair);
    if (config.translate.mymemory.email) url.searchParams.set('de', config.translate.mymemory.email);
    const res = await fetch(url, { signal: opts.signal });
    if (!res.ok) throw await upstreamError(res, 'MyMemory');
    const json = await res.json();
    if (json.responseStatus && Number(json.responseStatus) !== 200) {
      throw new HttpError(502, `MyMemory: ${json.responseDetails || json.responseStatus}`);
    }
    const out = json.responseData?.translatedText || '';
    if (out) onChunk(`${out} `);
  }
}

/** Provider finto per sviluppo/test: nessuna rete, streaming simulato. */
async function mockStream(opts, onChunk) {
  if (opts.detect) onChunk(`[[${baseLang(opts.from)}]]`);
  const words = `(${baseLang(opts.to)}) ${opts.text}`.split(' ');
  for (const w of words) {
    if (opts.signal?.aborted) return;
    await new Promise((r) => setTimeout(r, 15));
    onChunk(`${w} `);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Entry point
// ─────────────────────────────────────────────────────────────────────────────

export async function translateStream(opts) {
  const provider = config.translate.provider;
  const detect = opts.detect !== false && ['openai', 'anthropic', 'mock'].includes(provider);
  const o = { ...opts, detect };

  if (provider === 'deepl') {
    const out = await deeplTranslate(o);
    if (out) opts.onDelta(out);
    return;
  }
  if (provider === 'mymemory') {
    await mymemoryTranslate(o, opts.onDelta);
    return;
  }

  // Provider con tag di rilevamento lingua
  const parser = detect
    ? createTagParser((tag) => opts.onMeta?.({ detected: tag }), opts.onDelta)
    : { push: opts.onDelta, flush() {} };

  if (provider === 'mock') await mockStream(o, parser.push);
  else if (provider === 'openai') await openaiStream(o, buildPrompt(o), parser.push);
  else if (provider === 'anthropic') await anthropicStream(o, buildPrompt(o), parser.push);
  else throw new HttpError(500, `Provider di traduzione sconosciuto: ${provider}`);

  parser.flush();
}
