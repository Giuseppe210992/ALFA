# GPU Aggregator (universal_gpu_pool)

Router + worker effimeri + storage condiviso, con hand-off anticipato al 70% e watchdog (lease 45 s).
Solo libreria standard Python (PyTorch richiesto solo da `training_finetune`).

## Prove locali (nessuna GPU)
    python run_demo.py            # hand-off + crash di un nodo
    python run_demo.py --s3       # stessa prova su un S3 simulato (moto)
    python run_demo_launcher.py   # il launcher avvia i nodi da solo (backend simulato)

## Architettura "a telefono spento"
VM sempre accesa: router + launcher (+ Caddy per HTTPS) -> storage S3 -> worker su Kaggle avviati dal launcher.
File pronti in `deploy/` (Caddyfile, unit systemd, esempio di variabili d'ambiente).

1. **Bucket S3-compatibile** (R2/B2/...): metti bucket/endpoint in `pool_config.yaml`
   (`store_backend: s3`), le chiavi in `/etc/gpupool.env`.
2. **VM**: `pip install boto3 kaggle`, copia il progetto in /opt/universal_gpu_pool, imposta un `token` lungo,
   `router_public_url` (dominio HTTPS), `launcher_slots`. Avvia i servizi systemd.
3. **Kaggle (una tantum)**: `python run_launcher.py --dry-run` prepara i kernel; il primo `kaggle kernels push`
   li crea. Nelle impostazioni di OGNI notebook aggiungi i Secrets `POOL_TOKEN`, `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY` e attiva Internet + GPU. DA VERIFICARE: che i secrets restino collegati dopo i push successivi.
4. **Telefono**: apri `https://tuo-dominio/`, inserisci il token: stato, nodi, quota GPU, nuovo job.
   Notifica a fine job: imposta `notify_url` (es. un topic ntfy.sh segreto) e installa l'app ntfy.

## Non testato su servizi reali
Kaggle CLI (push/status), Secrets di Kaggle, R2/B2 reali, GPU reali, PyTorch. Testati con simulazione:
router, hand-off, watchdog, storage locale e S3 (moto), launcher e budget, pagina di stato.
Il budget GPU è una stima basata sui lanci; verifica la quota reale sul tuo account Kaggle.
Un solo account Kaggle: il launcher non gestisce più account.

## Nuovo modello
Crea `adapters/mio_adapter.py` (estendi `BaseAdapter`) e registralo in `adapters/__init__.py`.
