"""Demo: nessun worker acceso all'inizio. Il launcher (backend simulato) avvia i nodi da solo."""
import json, shutil, tempfile, threading, time
from core.aggregator_api import Router, make_server
from core.watchdog import Watchdog
from core.state_store import open_store
from core.launcher import Launcher
from core.launcher_backends import SimBackend
from pipelines.dag_runner import PoolClient, run_job
from worker.worker_daemon import Worker

root = tempfile.mkdtemp(prefix="gpu_pool_launch_")
cfg = dict(token="t", store_root=root, lease_timeout=1.5, handoff_threshold=0.7, session_limit_s=3600,
           max_attempts=5, default_vram_gb=14, launcher_slots=["u/w1", "u/w2", "u/w3"], max_nodes=2,
           gpu_hours_budget_week=20, launch_cooldown_s=0.5, startup_timeout_s=30)
store = open_store(cfg)
router = Router(cfg, store)
srv = make_server(router, "127.0.0.1", 0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
Watchdog(router, 0.3).start()
url = f"http://127.0.0.1:{srv.server_address[1]}"
client = PoolClient(url, "t")
launcher = Launcher(cfg, client, SimBackend(lambda slot: Worker(url, "t", store, slot, hb=0.3, poll=0.3,
                    verbose=False), max_idle_s=2), store, log=lambda m: print("[launcher]", m))
threading.Thread(target=lambda: [(launcher.tick(), time.sleep(0.4)) for _ in range(200)], daemon=True).start()

jid = run_job(client, "demo", [], {"n_steps": 6, "step_seconds": 1.0}, poll=0.5)
final = json.loads((store.fetch_output(jid, "step_006") / "state.json").read_text())
st = client.call("GET", "/status")
print("valore finale:", final, "| lanci:", len(launcher.state["launches"]), "| ore usate:", st["launcher"]["used_hours_week"])
assert final["value"] == 6 and len(launcher.state["launches"]) >= 2
# test quota: con budget esaurito non deve lanciare
launcher.budget_h = 0
before = len(launcher.state["launches"])
router.submit({"job_id": "j2", "adapter": "demo", "params": {"n_steps": 2, "step_seconds": 0.5},
               "steps": [{"id": "step_001", "depends_on": [], "payload": {"seconds": 0.5}}]})
time.sleep(4); launcher.tick()
assert len(launcher.state["launches"]) == before, "ha lanciato oltre il budget!"
print("OK: il launcher avvia i nodi da solo e rispetta il budget GPU")
shutil.rmtree(root, ignore_errors=True)
