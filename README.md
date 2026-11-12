# ALFA – Traduttore vocale hands-free (PWA)

PWA installabile su iPhone e Android per la **traduzione vocale bidirezionale in tempo reale**.
Si tocca **Avvia** una sola volta, poi l'app:

1. ascolta il microfono di continuo, anche se l'interlocutore parla senza pause;
2. traduce in streaming nella tua lingua e la **legge ad alta voce** con la voce più naturale disponibile;
3. quando rispondi nella tua lingua, traduce e legge la risposta all'interlocutore nella sua lingua, **senza altri tocchi**.

> Il tocco iniziale su "Avvia" non si può evitare: tutti i browser mobili richiedono un gesto
> dell'utente per attivare microfono e audio. Da quel momento in poi l'uso è completamente hands-free.

---

## Avvio rapido

Requisiti: **Node.js 22+**. Il progetto non ha dipendenze npm.

```bash
cp .env.example .env      # configura i provider (vedi sotto)
npm start                 # http://localhost:3000
npm test                  # 22 test (segmentatore, splitter, VAD, WAV, server)
```

Senza chiavi API l'app funziona subito: usa **MyMemory** (traduzione gratuita) e i motori vocali del browser.

### Sul tuo server (Oracle Cloud Free Tier o altro VPS) con il tuo dominio

```bash
# sul server, via SSH (Ubuntu: apt, Oracle Linux: dnf)
sudo apt install -y git        # oppure: sudo dnf install -y git
git clone -b ccr-05f08d52-6objp2 https://github.com/Giuseppe210992/ALFA.git alfa
cd alfa
sudo bash deploy/oracle-setup.sh traduttore.tuodominio.it
```
Lo script è pensato per server **già in uso** da altre app e non tocca nulla di esistente:
- usa un Node.js 22 privato in `/opt/alfa-node`;
- sceglie da solo una porta interna libera (da 3080 in su);
- se trova Caddy o nginx su 80/443 aggiunge solo la configurazione del dominio di ALFA, con validazione e backup;
- se le porte 80/443 sono libere installa Caddy;
- se trova Docker/Traefik non modifica nulla e stampa le istruzioni.

Su register.it serve un **record A** verso l'IP pubblico del server. Aggiornamenti: `bash deploy/update.sh`.

**Traduzione gratuita con FreeLLMAPI** (router compatibile OpenAI) nel file `.env`:
```
TRANSLATE_PROVIDER=openai
LLM_BASE_URL=https://ai.tuodominio.it/v1
LLM_API_KEY=la-tua-chiave-freellmapi
LLM_MODEL=gemini-3.1-flash-lite
```

### Pubblicarla online gratis (Render)

1. Vai su [render.com](https://render.com) ed entra con il tuo account GitHub.
2. **New → Blueprint** → scegli il repository `alfa` (il file `render.yaml` configura tutto).
3. Dopo 1-2 minuti ottieni un indirizzo tipo `https://alfa-translator.onrender.com`: aprilo dal telefono e installalo.

Il piano gratuito "si addormenta" dopo 15 minuti di inattività: la prima apertura successiva impiega ~50 secondi.

### Provarla sul telefono

Il microfono richiede **HTTPS** (fa eccezione `localhost`). Hai tre possibilità:

- **Tunnel**: `npx cloudflared tunnel --url http://localhost:3000` (oppure ngrok). Apri l'URL https dal telefono.
- **Deploy**: Render, Railway, Fly.io o un VPS con il `Dockerfile` incluso:
  ```bash
  docker build -t alfa . && docker run -p 3000:3000 --env-file .env alfa
  ```
- **Installazione**: su Android (Chrome) compare il banner "Installa". Su iPhone (Safari) usa Condividi → **Aggiungi alla schermata Home**.

---

## Configurazione consigliata

| Obiettivo | Impostazioni `.env` |
|---|---|
| Provare gratis | `TRANSLATE_PROVIDER=mymemory` |
| Traduzione di qualità a bassa latenza | `TRANSLATE_PROVIDER=openai` + Groq (`llama-3.1-8b-instant`) o OpenAI (`gpt-4o-mini`) |
| Traduzione con Claude | `TRANSLATE_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` |
| **iPhone** / lingua automatica | `STT_API_KEY` (Whisper OpenAI o `whisper-large-v3-turbo` su Groq) |
| Voci neurali non robotiche ovunque | `TTS_API_KEY` (`gpt-4o-mini-tts`) |

`OPENAI_API_KEY` fa da valore predefinito per LLM, STT e TTS. Se pubblichi il server su Internet,
imposta `APP_ACCESS_TOKEN` e inseriscilo nell'app (Impostazioni → Token di accesso).

---

## Struttura del progetto

```
├── server/                      Backend Node.js (zero dipendenze): serve la PWA e protegge le chiavi
│   ├── index.js                 HTTP server, router, file statici, header di sicurezza (CSP)
│   ├── config.js                Caricamento .env e stato dei provider
│   ├── lib/http.js              Body con limite, SSE, rate limit, parsing stream upstream
│   ├── lib/languages.js         Nomi delle lingue, normalizzazione della lingua di Whisper
│   └── providers/
│       ├── translate.js         OpenAI-compatibile · Anthropic · DeepL · MyMemory · mock (streaming)
│       └── speech.js            Whisper (/audio/transcriptions) e TTS neurale (/audio/speech)
├── public/                      Frontend PWA (JS vanilla, moduli ES, nessun build)
│   ├── index.html · css/styles.css
│   ├── manifest.webmanifest     Manifest di installazione (icone any + maskable)
│   ├── sw.js                    Service worker: shell offline, API sempre in rete
│   ├── icons/                   SVG sorgente + PNG generati (npm run icons)
│   └── js/
│       ├── app.js               Bootstrap, impostazioni, installazione PWA
│       ├── conversation.js      Orchestratore: STT → traduzione → TTS, turni, half-duplex, anti-eco
│       ├── stt/webspeech.js     SpeechRecognition con riavvio continuo automatico
│       ├── stt/segmenter.js     Taglio intelligente del parlato senza pause
│       ├── stt/cloud.js         Microfono → VAD → WAV → Whisper, con lingua automatica
│       ├── stt/audio-utils.js   VAD adattivo, ricampionamento, codifica WAV
│       ├── stt/pcm-worklet.js   AudioWorklet per la cattura PCM
│       ├── tts.js               Coda vocale, classifica delle voci naturali, TTS cloud
│       ├── api.js · settings.js · ui.js · languages.js · text-utils.js · wakelock.js
├── tests/                       node --test
├── scripts/generate-icons.mjs   Rendering delle icone PNG (Playwright)
└── Dockerfile · .env.example
```

---

## Come funziona

### 1. Ascolto continuo, anche senza pause
- **Motore browser (Web Speech API).** Il riconoscitore si riavvia da solo a ogni `end`, quindi l'ascolto non si interrompe mai. Su Android si usano sessioni brevi riavviate subito, perché lì `continuous=true` duplica i risultati.
  Il **segmentatore** (`stt/segmenter.js`) non aspetta il risultato "finale", che arriva solo dopo una pausa. Invia in anticipo il testo intermedio già stabile:
  - **per lunghezza**: tagliato prima di congiunzioni (*and, but, because, e, ma, perché…*) o dopo una virgola;
  - **per stabilità**: inviato quando il testo resta invariato per circa 0,9 s.

  Le ultime parole, che il riconoscitore corregge spesso, non vengono mai inviate in anticipo. Quando arriva il risultato finale si traduce solo la parte residua.
- **Motore cloud (Whisper).** Un VAD adattivo chiude il segmento dopo circa 550 ms di silenzio. Se chi parla non si ferma mai, a 4,5/7/10 s (in base alla reattività scelta) taglia nel **punto di minima energia** dell'ultimo secondo e mezzo, di solito uno spazio tra due parole, senza perdere audio. Le trascrizioni vengono consegnate in ordine.

### 2. Traduzione in streaming e voce naturale
- Il server inoltra lo stream dell'LLM via SSE. Il client spezza il testo in frasi e **avvia la sintesi alla prima frase completa**, mentre l'LLM sta ancora generando il resto.
- Il prompt include gli ultimi turni come contesto (pronomi, genere, registro) e chiede di correggere gli errori di riconoscimento.
- **Voci**: ogni voce di sistema riceve un punteggio. Sono preferite le voci *Neural/Natural* (Edge), *Premium/Enhanced/Siri* (iOS) e *Google* (Chrome); le voci "giocattolo" sono escluse. Puoi scegliere la voce a mano, o usare le voci neurali cloud con istruzioni di tono naturale.

### 3. Bidirezionale hands-free
- **Cloud**: Whisper rileva la lingua di ogni frase, quindi la direzione è automatica.
- **Browser** (una lingua alla volta):
  - dopo ogni traduzione l'app ascolta la lingua di **chi deve rispondere**;
  - se nessuno parla per 12 s, alterna la lingua;
  - l'LLM antepone un tag `[[xx]]` con la lingua effettiva della frase: se il riconoscitore ascoltava la lingua sbagliata, la direzione si inverte al volo, e il rumore (`[[??]]`) viene scartato;
  - il chip della lingua ascoltata permette, se vuoi, una correzione manuale.
- **Half-duplex**: in vivavoce il microfono resta in pausa mentre l'app parla, così non traduce sé stessa. Un **filtro anti-eco** scarta anche le frasi quasi identiche a quelle appena pronunciate. Con gli auricolari puoi attivare *Ascolto continuo* (full-duplex).
- **Wake Lock**: lo schermo resta acceso, altrimenti il sistema spegnerebbe il microfono.

### 4. PWA
- `manifest.webmanifest`: modalità standalone, icone normali e maskable, scorciatoia, meta tag per iOS.
- `sw.js`: precarica la shell, che si apre istantaneamente anche offline. Le API non passano mai dalla cache. Per pubblicare un aggiornamento basta incrementare `VERSION`.

---

## Compatibilità e limiti noti

| Piattaforma | Riconoscimento | Note |
|---|---|---|
| Chrome Android | Browser ✅ / Cloud ✅ | Esperienza migliore e gratuita con il motore browser |
| Safari iOS / PWA iOS | **Cloud consigliato** | In modalità standalone `SpeechRecognition` è assente o instabile |
| Chrome/Edge desktop | Browser ✅ / Cloud ✅ | Edge offre voci "Natural" eccellenti |
| Firefox | Solo Cloud | Nessuna SpeechRecognition |

- Con il motore browser, una frase pronunciata mentre l'app ascoltava l'altra lingua può andare persa. In quel caso il cambio lingua avviene comunque in automatico. Per una bidirezionalità perfetta usa il motore **Cloud**.
- In vivavoce, ciò che viene detto *mentre l'app parla* non viene ascoltato (half-duplex). Con gli auricolari attiva *Ascolto continuo*.
- iOS sospende microfono e audio quando l'app va in background o lo schermo si blocca.
