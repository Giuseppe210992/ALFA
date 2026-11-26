import argparse
from core.config import load_config
from core.state_store import open_store
from worker.worker_daemon import Worker

ap = argparse.ArgumentParser()
ap.add_argument("--router", required=True, help="es. https://xxxx.trycloudflare.com")
ap.add_argument("--config", default="config/pool_config.yaml")
ap.add_argument("--name", default="node")
ap.add_argument("--max-idle", type=int, default=600, help="esci dopo N secondi senza lavoro (risparmia quota GPU)")
a = ap.parse_args()
cfg = load_config(a.config)
Worker(a.router, cfg["token"], open_store(cfg), a.name, hb=cfg["heartbeat_interval"],
       session_limit_s=cfg["session_limit_s"]).run(max_idle_s=a.max_idle)
