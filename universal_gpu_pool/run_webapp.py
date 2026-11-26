#!/usr/bin/env python3
"""Avvia la webapp GPU Pool: router + worker locale (API free) + interfaccia nel browser.

Uso:
    python run_webapp.py
    python run_webapp.py --port 8765
    python run_webapp.py --no-browser

Il worker locale esegue i job remote_api sullo stesso host del router
(cosi non servono nodi Kaggle ne passare le API key ai notebook).

Variabili utili:
    CEREBRAS_API_KEY / OPENROUTER_API_KEY / SCALEWAY_API_KEY
"""
import argparse
import os
import secrets
import threading
import time
import webbrowser


def main():
    ap = argparse.ArgumentParser(description="GPU Pool — webapp")
    ap.add_argument("--config", default="config/pool_config.yaml")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--token", default=None)
    ap.add_argument("--no-local-worker", action="store_true",
                    help="non avviare il worker locale (solo router)")
    args = ap.parse_args()

    from core.config import load_config
    from core.aggregator_api import Router, make_server
    from core.watchdog import Watchdog
    from core.state_store import open_store

    cfg = load_config(args.config)
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    if args.token:
        cfg["token"] = args.token

    # Sicurezza: token di default prevedibile
    if not cfg.get("token") or cfg.get("token") in ("change-me", "changeme", "token"):
        generated = secrets.token_urlsafe(32)
        cfg["token"] = generated
        print()
        print("  [!] Token di default insicuro: ne ho generato uno per questa sessione.")
        print(f"  [!] Token: {generated}")
        print("  [!] Salvalo in config/pool_config.yaml per le prossime volte.")
        print()

    host = cfg.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        print("  [!] host=0.0.0.0 espone il servizio su tutte le interfacce.")
        print("  [!] Usa HTTPS (Caddy) e un token forte prima di esporre su Internet.")
        print()

    open_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    port = int(cfg.get("port") or 8765)
    url = f"http://{open_host}:{port}/"

    keys = [k for k in ("CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "SCALEWAY_API_KEY") if os.environ.get(k)]
    if not keys:
        print("  [!] Nessuna API key free trovata nell'ambiente.")
        print("  [!] export CEREBRAS_API_KEY=...  oppure OPENROUTER_API_KEY=...")
        print()
    else:
        print(f"  API key rilevate: {', '.join(keys)}")

    store = open_store(cfg)
    router = Router(cfg, store=store)
    Watchdog(router, interval=5).start()
    server = make_server(router, host, port)

    start_local = cfg.get("local_api_worker", True) and not args.no_local_worker
    if start_local:
        def _local_worker():
            time.sleep(0.4)
            try:
                from worker.worker_daemon import Worker
                base = f"http://127.0.0.1:{port}"
                w = Worker(base, cfg["token"], store, name="local-api",
                           hb=max(5, int(cfg.get("heartbeat_interval", 10))),
                           poll=1.5, session_limit_s=24 * 3600, verbose=True)
                w.run(max_idle_s=None)
            except Exception as e:
                print(f"  [!] Worker locale terminato: {e}")

        threading.Thread(target=_local_worker, daemon=True, name="local-api-worker").start()
        print("  Worker locale: attivo (path API free senza GPU esterne)")

    def _open():
        time.sleep(0.7)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if not args.no_browser:
        threading.Thread(target=_open, daemon=True).start()

    print()
    print("  ==========================================")
    print("       GPU Pool — Webapp avviata")
    print("  ==========================================")
    print(f"  Interfaccia:  {url}")
    print(f"  Token:        {cfg.get('token', '')}")
    print(f"  Store:        {cfg.get('store_root', '')}")
    print()
    print("  Percorso gratis: nell'UI scegli «Fai una domanda»")
    print("  Ctrl+C per terminare.")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArresto webapp.")
        server.shutdown()


if __name__ == "__main__":
    main()
