import argparse, sys
from core.config import load_config
from core.aggregator_api import Router, make_server
from core.watchdog import Watchdog

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="config/pool_config.yaml")
a = ap.parse_args()
cfg = load_config(a.config)
r = Router(cfg)
Watchdog(r, interval=5).start()
srv = make_server(r, cfg["host"], cfg["port"])
print(f"Router in ascolto su {cfg['host']}:{cfg['port']} | store: {cfg['store_root']}")
srv.serve_forever()
