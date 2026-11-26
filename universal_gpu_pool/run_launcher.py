import argparse
from core.config import load_config
from core.state_store import open_store
from core.launcher import Launcher
from core.launcher_backends import KaggleBackend
from pipelines.dag_runner import PoolClient

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="config/pool_config.yaml")
ap.add_argument("--dry-run", action="store_true", help="genera i file del kernel senza lanciare nulla")
a = ap.parse_args()
cfg = load_config(a.config)
backend = KaggleBackend(cfg)
problems = backend.check()
if problems:
    print("Configurazione Kaggle incompleta:")
    for pr in problems:
        print("  -", pr)
    if not a.dry_run:
        raise SystemExit(2)
if a.dry_run:
    for s in cfg["launcher_slots"]:
        print("preparato:", backend.prepare(s))
    raise SystemExit
Launcher(cfg, PoolClient(cfg["router_url"], cfg["token"]), backend, open_store(cfg)).run(cfg.get("launcher_interval_s", 60))
