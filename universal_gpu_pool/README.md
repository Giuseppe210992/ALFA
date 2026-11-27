---
title: GPU Pool
emoji: 🖥️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# ▶️ Avvia l'app con un tocco — gratis, senza carta, senza installare nulla

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Giuseppe210992/ALFA/tree/claude/new-session-q8o2nc?quickstart=1)

> 📲 **Icona sulla schermata Home:** dal telefono, a app aperta, usa il banner **«Installa»** (Android) oppure
> Safari → Condividi → *Aggiungi alla schermata Home* (iPhone). Da quel momento si apre con un tocco, a schermo intero.
> L'icona funziona bene con un **indirizzo stabile** (copia su Hugging Face, vedi `deploy/huggingface/LEGGIMI.md`):
> l'indirizzo di Codespaces cambia a ogni riavvio.

Funziona da **telefono o computer**:

1. **Tocca il pulsante** e accedi con il tuo account GitHub (gratuito: basta l'email, nessuna carta).
2. GitHub prepara per te un piccolo computer in cloud (1-2 minuti) e **avvia l'app da solo**.
3. Si apre una pagina con un **grande link 👉 «TOCCA QUI PER APRIRE L'APP»**: toccalo (se chiede conferma, tocca *Apri*).
   Il sito si apre e **si attiva da solo**. *(Se vedi solo righe di codice, apri il file `APRI-L-APP.md`.)*
4. Il sito ti guida: collega **Kaggle** (circa 30 ore di GPU gratis a settimana) e scegli cosa fare. Fine.

Costo: **zero**. GitHub regala a ogni account circa **60 ore al mese** di questo computer in cloud: quando non lo usi, spegnilo
(github.com/codespaces → ⋯ → *Stop codespace*). Passi illustrati e consigli: [`deploy/codespaces/INIZIA-QUI.md`](deploy/codespaces/INIZIA-QUI.md).

*Preferisci il computer di casa?* Scarica lo ZIP (Code → Download ZIP), estrailo e fai doppio clic su `avvia.bat` (Windows) o `avvia.command` (Mac).
*Vuoi una copia su un server gratuito di Hugging Face?* → [`deploy/huggingface/LEGGIMI.md`](deploy/huggingface/LEGGIMI.md).

---

# GPU Pool (universal_gpu_pool)

Webapp per usare modelli AI grandi **gratis**: le richieste vanno alle API gratuite (Groq, Cerebras,
Google Gemini, OpenRouter, Scaleway o un tuo endpoint OpenAI-compatibile) con rotazione automatica
quando un servizio finisce la quota. Opzionale: nodi GPU gratuiti su Kaggle per modelli locali.

Serve solo **Python 3.8+** (funziona anche su Windows 7). Nessuna libreria da installare per l'uso normale.

---

## Avvio rapido (5 minuti)

1. **Avvia**
   - Windows: doppio clic su `avvia.bat`
   - macOS: doppio clic su `avvia.command` (la prima volta: tasto destro → Apri)
   - Linux / terminale: `./avvia.sh` oppure `python3 run_webapp.py`

   Si apre il browser **già autenticato**. Se non si apre, copia il link `Apri: http://127.0.0.1:8765/#t=…`
   che compare nel terminale.

2. **Collega un account gratuito** (il pannello «Account e chiavi» si apre da solo al primo avvio)
   - clicca **«Ottieni chiave gratis ↗»** accanto a uno dei servizi, crea la chiave sul loro sito;
   - incollala nel campo e premi **Salva**: parte subito una **Prova** e vedi «✓ Funziona!».

   | Servizio | Dove prendere la chiave | Modello predefinito |
   |---|---|---|
   | Groq | https://console.groq.com/keys | `openai/gpt-oss-120b` |
   | Cerebras | https://cloud.cerebras.ai/ | `gpt-oss-120b` |
   | Google Gemini | https://aistudio.google.com/apikey | `gemini-2.5-flash` |
   | OpenRouter | https://openrouter.ai/keys | `openrouter/free` (sceglie un modello gratuito) |
   | Scaleway | console Scaleway → Generative APIs | `qwen3-235b-a22b-instruct-2507` (credito iniziale) |
   | Personalizzato | il tuo server (FreeLLMAPI, LiteLLM, Ollama…) | URL base `…/v1` + modello + chiave |

   Più account colleghi, più richieste gratuite al giorno: il pool li alterna da solo.

3. **Usa**: «Fai una domanda» oppure «Analizza tanti testi» (una riga = un elemento) → Avvia.
   I risultati compaiono in basso; puoi copiarli o scaricarli in **CSV/JSON**. Se qualche elemento
   fallisce (es. quota finita), «↻ Riprova falliti» rilancia solo quelli.

Le chiavi restano **solo sul tuo computer** in `config/secrets.env` (esclusa da git, permessi 600).
Il token di accesso è in `config/local_config.yaml`. Nessuno dei due file va condiviso.

### Dal telefono (stessa Wi-Fi)
Avvia **`avvia-telefono.bat`** (Windows) o `avvia-telefono.command` (macOS) invece di `avvia`. Nel terminale compare l'indirizzo da aprire sul telefono
(oppure, nella webapp, il pulsante **«📱 Telefono»**): contiene già il token, quindi entri senza digitare nulla; puoi inviartelo su WhatsApp/Telegram.
Il telefono deve essere sulla **stessa Wi-Fi** del computer, che resta acceso con la finestra nera aperta; alla prima richiesta di Windows scegli «Consenti».
Dal telefono si possono scegliere anche foto e video dalla galleria. Il collegamento non è cifrato (HTTP): usalo solo su una Wi-Fi fidata.
**Fuori casa (rete dati) con il PC non funziona**: servirebbe un tunnel (Cloudflare/Tailscale) che non gira su Windows 7.
Per usarlo ovunque e a PC spento, su un **server gratuito senza carta** (Hugging Face Spaces): guida in `deploy/huggingface/LEGGIMI.md`
(`Dockerfile` e intestazione di questo file già pronti; protezioni per internet: segreto `GPOOL_TOKEN` obbligatorio, blocco dopo 8 codici sbagliati,
cancellazione automatica di eventuali file con chiavi caricati per errore). **Non verificato su Hugging Face vero** e l'immagine Docker non è stata costruita qui.

### Problemi comuni
- **«Nessuna chiave API configurata»** → apri «Account e chiavi» e collega almeno un servizio.
- **Prova → HTTP 401/403** → chiave sbagliata o revocata: incollane una nuova.
- **Prova → HTTP 404** → il modello non esiste più su quel servizio: «Impostazioni avanzate» → Modello
  (le liste gratuite cambiano spesso; controlla sul sito del servizio).
- **«Tutti i provider sono in pausa»** → quote giornaliere finite: collega un altro servizio o riprova domani.
- **Porta occupata** → la webapp ne sceglie un'altra da sola (guarda il link nel terminale).
- Nota: Qwen3-235B non è più gratuito su Cerebras/OpenRouter (verificato 26/11/2026). I modelli predefiniti
  sono quelli realmente gratuiti oggi; puoi cambiarli dal pannello.

---

## Test

    python run_tests.py          # 107 test (backend API, router, worker, webapp via HTTP) + demo originali
    python run_tests.py --e2e    # + test nel browser su una copia pulita (Node + Playwright)

I test usano un finto server OpenAI (`tests/fake_openai.py`): nessuna rete, nessuna chiave reale.

### Demo originali (nessuna GPU)
    python run_demo.py            # hand-off + crash di un nodo
    python run_demo.py --s3       # stessa prova su un S3 simulato (richiede: pip install "moto[server]" boto3)
    python run_demo_launcher.py   # il launcher avvia i nodi da solo (backend simulato)

---

## Come funziona

- `run_webapp.py` avvia nello stesso processo: router HTTP (`core/aggregator_api.py`), watchdog e un
  **worker locale solo-API** che esegue i lavori via API gratuite (non serve GPU).
- `core/remote_api_backend.py`: rotazione provider, limiti al minuto/giorno salvati su disco, pausa su 429
  (rispetta `Retry-After`; quota giornaliera → pausa fino a mezzanotte UTC), disattivazione su 401/403/404,
  cache delle risposte (stessa domanda = zero quota). Provider in `config/providers.json` (creato al primo avvio).
- I job «Usa la GPU del pool» restano in coda finché non si collega un nodo GPU: il worker solo-API non li prende.
- Hand-off anticipato al 70%, lease 45 s, commit a due fasi: vedi demo e `core/state_store.py`.

## Programma da GitHub: incolli il link e parte (senza scrivere codice)

1. **Collega Kaggle** (una volta): «Account e chiavi» → sezione **GPU in cloud** → segui i 4 passi (account gratuito,
   verifica del telefono, «Generate New Token» su kaggle.com/settings/api, incolla il token). Si vedono le ore GPU rimaste
   (circa 30 a settimana, gratis). Serve anche un account IA (Groq/Cerebras/Gemini) per l'analisi migliore, ma non è obbligatorio.
2. **«Programma da GitHub»** → incolla il link del progetto → **Analizza**. Il sistema legge README ed esempi, spiega in
   italiano cosa fa il programma e **genera da solo il modulo** con i campi da compilare (testo, link a un video, un numero…).
3. **Continua → Avvia.** Il programma viene scaricato, installato ed eseguito su una GPU gratuita di Kaggle **nel tuo account**;
   i file prodotti compaiono in «Risultati» con i pulsanti di download, insieme al registro (`run.log`) per capire cosa è successo.

Come funziona: il tuo computer non fa calcoli e **non deve essere raggiungibile da internet**. Manda a Kaggle uno script che clona
il repository, esegue i comandi di installazione, scarica i file di input, lancia il comando e raccoglie i risultati; poi li scarica.
Il collegamento a Kaggle usa le stesse chiamate dell'SDK ufficiale ma con la sola libreria standard, quindi funziona con Python 3.8/Windows 7.
Se manca un account IA, l'analisi usa regole semplici (comandi d'esempio del README, argparse): il comando va controllato in «Dettagli tecnici».

**Limiti onesti**
- **Colab non è collegabile**: Google non offre un'interfaccia per avviarlo da un programma. Si usa Kaggle (che la offre).
- **Non tutti i progetti funzionano al primo colpo**: dipendono da pesi enormi, versioni precise di librerie, interfacce web (gradio)…
  Se fallisce, in «Risultati» vedi le ultime righe del registro; «↻ Riprova» rilancia, e in «Dettagli tecnici» puoi correggere comandi.
- **File dal tuo computer: si trascinano nella webapp** (zona tratteggiata «Trascina qui un file», nel modo 3D e in ogni campo «link a un file»;
  fino a 5 GB). Il file va prima al programma sul tuo PC, che lo carica come **dataset privato e temporaneo** nel tuo account Kaggle; il notebook lo
  trova da solo e, a fine lavoro, il dataset viene cancellato. In alternativa si può incollare un link (anche quelli di Drive/Dropbox).
  Non verificato su Kaggle reale: i tempi di elaborazione del dataset e la posizione in cui Kaggle lo monta (lo script la cerca da solo).
- **Esegui solo progetti di cui ti fidi**: girano nel tuo account Kaggle (sandbox di Kaggle, non sul tuo PC).
- Solo repository **pubblici**. GitHub concede 60 analisi/ora senza account (variabile `GITHUB_TOKEN` per alzare il limite).
- Kaggle: 2 sessioni GPU in contemporanea, 9 ore massime ciascuna. L'annullamento di un lavoro dalla webapp non ferma il notebook già partito.

**Verificato**: analisi con un modello IA **vero** (Qwen 3.8 su Groq) su un repository di prova: piano valido con campi in italiano;
protocollo Kaggle copiato dall'SDK ufficiale (kagglesdk 0.1.38) e provato contro un finto Kaggle che **esegue davvero** lo script generato
(clone git, installazione, download input, esecuzione, raccolta file, errori, timeout, paginazione); tentativi di iniezione nel testo
dell'utente neutralizzati; l'intero percorso nel browser (token → analisi → modulo → esecuzione → download). Eseguito su Python 3.13 e 3.8.
**Non verificato**: nessun test contro il vero Kaggle (da qui non è raggiungibile) né contro l'API di GitHub (l'ambiente di sviluppo la
blocca per i repository di terzi). Il primo collegamento reale potrebbe richiedere un piccolo aggiustamento: se vedi un errore da Kaggle,
mandami il messaggio. Per provare l'account reale: `KAGGLE_API_TOKEN_REAL=<token> python -m unittest tests.test_github_runner.RealKaggleTest`.

## Ricostruzione 3D con LingBot-Map

Dalla webapp: **«Ricostruzione 3D»** → incolla il link del video (o di uno **ZIP** di foto) → Avvia. Gira su una GPU gratuita
di Kaggle (serve l'account Kaggle collegato), non richiede nodi nel pool. I link copiati da **Google Drive**
(«Chiunque abbia il link») e **Dropbox** vengono convertiti da soli in link diretti; le cartelle di Drive non si possono scaricare (crea uno ZIP).
Le foto vengono **preparate prima del modello**: il `demo.py` ufficiale di LingBot cerca solo `*.jpg`, `*.png`, `*.JPG` senza guardare nelle
sottocartelle (foto `.jpeg`/`.PNG`/`.webp` o ZIP con cartelle risultavano «0 immagini»). Il passo di preparazione le trova ovunque, le ordina,
corregge la rotazione dei telefoni e riduce quelle enormi; se non ne trova dice cosa contiene lo ZIP. Servono almeno 3 foto (meglio 20+, con molta sovrapposizione).
Su Kaggle FlashInfer non c'è: si usa l'attenzione SDPA (più lenta ma equivalente). Non verificato su Kaggle reale: compatibilità
della versione di PyTorch di Kaggle con LingBot-Map. Qui sotto, il funzionamento con nodi GPU propri (runtime «pool»).

### Ricostruzione 3D con un nodo GPU del pool (avanzato)

[LingBot-Map](https://github.com/Robbyant/lingbot-map) (Apache-2.0) ricostruisce una scena 3D da un video o da foto:
posizione della camera e nuvola di punti. Nel pool è l'adapter `lingbot_map`; nella webapp è **«Ricostruzione 3D»**.

- **Input**: un link diretto a un video (mp4, mov…) oppure a uno **ZIP di foto** (jpg/png).
- **Dove gira**: solo su un nodo con GPU NVIDIA (Kaggle/Colab). Il worker «Questo computer» non ha GPU e **non**
  prende questi lavori: finché non c'è un nodo GPU il lavoro resta «in coda» e la webapp lo segnala.
- **Cosa fa il nodo da solo**: clona il repository, installa le librerie leggere mancanti, scarica i pesi
  `lingbot-map.pt` (~4,6 GB, solo la prima volta di ogni sessione) ed esegue il `demo.py` ufficiale senza
  interfaccia grafica (il visualizzatore viser è sostituito da un salvataggio su file). PyTorch con CUDA deve già esserci.
- **Risultato** (pulsanti di download in «Risultati»): `scene.glb` (nuvola di punti + traiettoria, si apre con Blender,
  MeshLab o gltf-viewer.donmccurdy.com), `camera_poses.json`, `predictions.npz` (pose, profondità, confidenza), `run.log`.
- **Opzioni** (modalità esperta → `params.options`): `fps`, `stride`, `first_k`, `mode` (`streaming`/`windowed` per
  oltre ~3000 fotogrammi), `keyframe_interval`, `num_scale_frames` (2 se la GPU va in memoria piena),
  `checkpoint` (`lingbot-map-long.pt`), `use_sdpa`, `repo_dir`, `model_path`. Elenco completo in `adapters/lingbot_map_adapter.py`.
- **LingBot-Map 2.0** esiste (`Robbyant/lingbot-map-v2`) ma al 27/11/2026 i suoi pesi sono *privati* (serve un account
  con accesso) e la licenza è CC BY-NC (solo uso non commerciale): per questo l'adapter usa la 1.0.

**Verificato**: salvataggio dei risultati con il vero modulo di esportazione GLB di LingBot-Map su dati sintetici;
tutta la catena (download video/ZIP da URL, ZIP non sicuri rifiutati, avanzamento, errori spiegati, job in coda
finché non c'è una GPU, elenco e download dei file, percorsi non consentiti respinti) con un finto repository
(`tests/stub_lingbot`); le opzioni passate a `demo.py` esistono nel `demo.py` reale.
**Non verificato** (serve una GPU): l'esecuzione del modello vero. Per provarla su una macchina con GPU:
`LINGBOT_MAP_DIR=/percorso/lingbot-map LINGBOT_MAP_IMAGES=/percorso/foto python -m unittest tests.test_lingbot.RealRepoTest`.
Limite attuale: il video/le foto vanno raggiunti con un link; non c'è ancora il caricamento di file dalla webapp.

## Nodi GPU gratuiti su Kaggle (opzionale, avanzato)

Serve una macchina sempre accesa raggiungibile in HTTPS e uno storage condiviso S3-compatibile.

1. **Storage** (R2/B2/S3/MinIO): in `config/pool_config.yaml` imposta `store_backend: s3`, bucket ed endpoint;
   le chiavi vanno in variabili d'ambiente (`deploy/gpupool.env.example`), mai nel file di config.
2. **VM**: `pip install boto3 kaggle`, copia il progetto in /opt/universal_gpu_pool, imposta
   `router_public_url` (HTTPS: Caddy con `deploy/Caddyfile` o un tunnel), `launcher_slots`
   (`tuo_username/gpupool-w1`…), servizi systemd in `deploy/`.
3. **Kaggle (una tantum)**: `python run_launcher.py --dry-run` controlla la configurazione e prepara i kernel;
   il primo `kaggle kernels push` li crea. In OGNI notebook aggiungi i Secrets `POOL_TOKEN`,
   `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` e attiva Internet + GPU.
4. `python run_launcher.py` (o il servizio systemd): avvia i nodi quando c'è lavoro GPU, nei limiti di
   `max_nodes` e `gpu_hours_budget_week`. Se la configurazione è incompleta si rifiuta e spiega cosa manca.

Verificato in locale: il kernel generato scompatta il codice, si registra al router, esegue un job ed esce
quando è inattivo. **Non verificato su servizi reali**: CLI Kaggle (push/status), Secrets Kaggle, R2/B2 reali,
GPU reali, PyTorch/transformers. Il budget GPU è una stima basata sui lanci: controlla la quota reale su Kaggle.

## Sicurezza
- Il server ascolta solo su `127.0.0.1` di default. Per esporlo usa HTTPS (Caddy) e un token lungo.
- Il runtime `command` e gli adapter `chunked`/`training_finetune` eseguono codice: usali solo per job fidati.
- Le chiavi non vengono mai rimandate al browser (si vedono solo le ultime 4 cifre).

## Nuovo modello / adapter
Crea `adapters/mio_adapter.py` (estendi `BaseAdapter`) e registralo in `adapters/__init__.py`.
