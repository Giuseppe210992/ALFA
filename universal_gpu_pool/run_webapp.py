#!/usr/bin/env python3
"""Avvia la webapp GPU Pool: router + worker locale (API free) + interfaccia nel browser.

Uso:
    python run_webapp.py                 # apre il browser già autenticato
    python run_webapp.py --port 8765
    python run_webapp.py --no-browser

Al primo avvio genera un token sicuro e lo salva in config/local_config.yaml.
Le chiavi API si inseriscono dalla webapp («Account e chiavi»): vengono salvate in
config/secrets.env (solo su questo computer) e usate subito, senza riavviare.
In alternativa funzionano anche le variabili d'ambiente (CEREBRAS_API_KEY, GROQ_API_KEY, ...).

Il worker locale esegue i job remote_api sullo stesso host del router
(così non servono nodi Kaggle né passare le API key ai notebook).
"""
import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
WEAK_TOKENS = ("", "change-me", "changeme", "token", None)


def _port_free(host, port):
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main():
    if sys.version_info < (3, 8):
        sys.exit("Serve Python 3.8 o più recente (hai %d.%d). Scaricalo da https://www.python.org/downloads/"
                 % sys.version_info[:2])
    # Tutti i percorsi (config/, store) sono relativi alla cartella del progetto,
    # qualunque sia la cartella da cui si lancia lo script (doppio clic incluso).
    os.chdir(HERE)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        sys.stdout.reconfigure(errors="replace")   # console Windows senza UTF-8
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="GPU Pool — webapp")
    ap.add_argument("--config", default="config/pool_config.yaml")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--token", default=None)
    ap.add_argument("--lan", action="store_true",
                    help="rendi la webapp raggiungibile dal telefono (stessa rete Wi-Fi)")
    ap.add_argument("--no-local-worker", action="store_true",
                    help="non avviare il worker locale (solo router)")
    args = ap.parse_args()

    from core.config import load_config, save_local
    from core.aggregator_api import Router, make_server
    from core.watchdog import Watchdog
    from core.state_store import open_store
    from core.secrets_store import load_into_env, SECRETS_FILE
    from core.remote_api_backend import load_providers, save_providers, PROVIDERS_FILE

    cfg = load_config(args.config)
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    elif os.environ.get("GPOOL_PORT"):
        cfg["port"] = int(os.environ["GPOOL_PORT"])
    if args.token:
        cfg["token"] = args.token
    if args.lan:
        cfg["host"] = "0.0.0.0"
    # Server in cloud (es. Hugging Face Spaces): token e opzioni arrivano da «segreti» / variabili d'ambiente
    public = bool(os.environ.get("GPOOL_PUBLIC") or os.environ.get("SPACE_ID"))
    if os.environ.get("GPOOL_TOKEN") and not args.token:
        cfg["token"] = os.environ["GPOOL_TOKEN"].strip()
    if public:
        cfg["token"] = os.environ.get("GPOOL_TOKEN", "").strip()      # in cloud il token viene SOLO dal segreto, mai da file
        for leftover in ("secrets.env", "local_config.yaml", "providers.json"):
            f = os.path.join(HERE, "config", leftover)
            if os.path.exists(f):                 # arrivato dentro il pacchetto caricato: non va MAI usato né lasciato
                os.remove(f)
                print(f"  [!] Cancellato config/{leftover} trovato nel pacchetto caricato.")
                print("      Se conteneva chiavi vere, REVOCALE e rigenerale (erano visibili a chi ha accesso ai file del Space).")
        cfg["trust_proxy"] = True            # dietro il proxy del servizio: l'IP vero sta in X-Forwarded-For
        cfg["verbose_worker"] = False
        if len(str(cfg.get("token") or "")) < 24 or cfg.get("token") in WEAK_TOKENS:
            print("\n  [!] Server pubblico: manca il segreto GPOOL_TOKEN (almeno 24 caratteri).")
            print("      Impostalo in Settings → Variables and secrets del tuo Space e riavvia.\n")
            sys.exit(2)

    # Token: generato una volta sola e salvato in config/local_config.yaml (escluso da git)
    if cfg.get("token") in WEAK_TOKENS and not public:
        cfg["token"] = secrets.token_urlsafe(32)
        save_local(args.config, {"token": cfg["token"]})
        print("  Token generato e salvato in config/local_config.yaml")

    # Chiavi salvate dalla webapp + file dei provider (creato con i valori predefiniti)
    loaded = load_into_env()
    if not PROVIDERS_FILE.exists():
        save_providers(load_providers())

    host = cfg.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::") and not args.lan:
        print("  [!] host=0.0.0.0 espone il servizio su tutte le interfacce.")
        print("  [!] Usa HTTPS (Caddy) e un token forte prima di esporre su Internet.")
        print()

    port = int(cfg.get("port") or 8765)
    if not args.port:
        for cand in range(port, port + 20):
            if _port_free(host, cand):
                if cand != port:
                    print(f"  Porta {port} occupata: uso {cand}")
                port = cand
                break
    cfg["port"] = port
    open_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    base_url = f"http://{open_host}:{port}"

    providers = load_providers()
    with_key = [p.label or p.name for p in providers if p.enabled and p.base_url and p.has_key()]

    # Cache risposte + contatori di quota: nell'archivio del progetto (sopravvive ai riavvii)
    if cfg.get("store_backend", "local") != "s3":
        os.environ.setdefault("GPU_POOL_API_CACHE",
                              os.path.abspath(os.path.join(str(cfg.get("store_root") or "gpu_pool_store"), "api_cache")))

    store = open_store(cfg)
    router = Router(cfg, store=store)
    Watchdog(router, interval=5).start()
    try:
        server = make_server(router, host, port)
    except OSError as e:
        print(f"  [!] Impossibile aprire {host}:{port}: {e}")
        print("      Prova con: python run_webapp.py --port 8800")
        sys.exit(1)

    start_local = cfg.get("local_api_worker", True) and not args.no_local_worker
    if start_local:
        def _local_worker():
            from worker.worker_daemon import Worker
            time.sleep(0.4)
            while True:     # se il worker termina (errore o fine sessione) ne parte un altro
                try:
                    w = Worker(base_url, cfg["token"], store, name="local-api", api_only=True,
                               hb=max(5, int(cfg.get("heartbeat_interval", 10))),
                               poll=1.0, session_limit_s=24 * 3600, verbose=bool(cfg.get("verbose_worker")))
                    w.run(max_idle_s=None)
                except Exception as e:
                    print(f"  [!] Worker locale: {e} (riavvio tra 5 s)")
                time.sleep(5)

        threading.Thread(target=_local_worker, daemon=True, name="local-api-worker").start()

    # Il link contiene il token nel frammento (#): resta nel browser, non viene inviato in rete.
    login_url = f"{base_url}/#t={cfg['token']}" if not public else f"{base_url}/"

    def _open():
        time.sleep(0.8)
        try:
            webbrowser.open(login_url)
        except Exception:
            pass

    if not args.no_browser:
        threading.Thread(target=_open, daemon=True).start()

    print()
    print("  =========================================")
    print("        GPU Pool — Webapp avviata")
    print("  =========================================")
    if public:
        print(f"  Server pubblico in ascolto su {base_url}/ — accesso con il segreto GPOOL_TOKEN (non stampato).")
    else:
        print(f"  Apri:        {login_url}")
        print(f"  (indirizzo:  {base_url}/  — token in config/local_config.yaml)")
    if args.lan:
        from core.netutil import lan_ip
        ip = lan_ip()
        print()
        if ip:
            print("  DAL TELEFONO (collegato alla STESSA Wi-Fi del computer), apri:")
            print(f"     http://{ip}:{port}/#t={cfg['token']}")
            print("  Se Windows chiede di consentire l'accesso, scegli «Consenti» (rete privata).")
        else:
            print("  [!] Non riesco a trovare l'indirizzo del computer nella rete: sei collegato alla Wi-Fi?")
        print("  [!] Il collegamento non è cifrato: usalo solo su una Wi-Fi di cui ti fidi.")
        print()
    print(f"  Archivio:    {os.path.abspath(str(cfg.get('store_root', '')))}")
    if with_key:
        print(f"  Account API: {', '.join(with_key)}")
    else:
        print("  Account API: nessuno. Nella webapp apri «Account e chiavi» e incolla una chiave")
        print("               gratuita (Groq, Cerebras o Gemini): bastano 2 minuti.")
    if loaded:
        print(f"  Chiavi caricate da {os.path.relpath(SECRETS_FILE)}: {', '.join(loaded)}")
    print("  Worker locale: " + ("attivo (API gratuite, nessuna GPU necessaria)" if start_local else "disattivato"))
    print()
    print("  Ctrl+C per terminare.")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArresto webapp.")
        server.shutdown()


if __name__ == "__main__":
    main()
