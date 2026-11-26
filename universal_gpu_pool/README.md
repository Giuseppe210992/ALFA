# GPU Pool (universal_gpu_pool)

Webapp per usare modelli AI grandi **gratis**: le richieste vanno alle API gratuite (Groq, Cerebras,
Google Gemini, OpenRouter, Scaleway o un tuo endpoint OpenAI-compatibile) con rotazione automatica
quando un servizio finisce la quota. Opzionale: nodi GPU gratuiti su Kaggle per modelli locali.

Serve solo **Python 3.9+**. Nessuna libreria da installare per l'uso normale.

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

    python run_tests.py          # 28 test (backend API, router, worker, webapp via HTTP) + demo originali
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
