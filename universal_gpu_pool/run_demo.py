"""Demo end-to-end: 3 worker simulati, catena di 6 step, il nodo A muore all'80% del suo primo step."""
import os, shutil, sys, threading, time, tempfile
from core.aggregator_api import Router, make_server
from core.watchdog import Watchdog
from core.state_store import open_store
from pipelines.dag_runner import PoolClient, run_job
from worker.worker_daemon import Worker

root = tempfile.mkdtemp(prefix="gpu_pool_demo_")
cfg = dict(token="t", store_root=root, lease_timeout=1.5, handoff_threshold=0.7, session_limit_s=3600,
           max_attempts=5, default_vram_gb=14)
if "--s3" in sys.argv:   # prova con un S3 finto (moto) invece della cartella locale
    from moto.server import ThreadedMotoServer
    import boto3
    s3srv = ThreadedMotoServer(port=0, verbose=False); s3srv.start()
    h, p = s3srv.get_host_and_port()
    os.environ.update(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", AWS_DEFAULT_REGION="us-east-1")
    boto3.client("s3", endpoint_url=f"http://{h}:{p}").create_bucket(Bucket="pool")
    cfg.update(store_backend="s3", s3_bucket="pool", s3_prefix="t", s3_endpoint_url=f"http://{h}:{p}")
store = open_store(cfg)
router = Router(cfg, store)
srv = make_server(router, "127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
Watchdog(router, interval=0.3).start()
url = f"http://127.0.0.1:{port}"

ws = [Worker(url, "t", store, n, hb=0.3, poll=0.3, session_limit_s=3600, crash_at=c)
      for n, c in (("A", ("*", 0.8)), ("B", None), ("C", None))]
for w in ws:
    threading.Thread(target=w.run, kwargs={"max_idle_s": 25}, daemon=True).start()
    time.sleep(0.2)

client = PoolClient(url, "t")
jid = run_job(client, "demo", [], {"n_steps": 6, "step_seconds": 1.5}, poll=0.5)
import json
final = json.loads((store.fetch_output(jid, "step_006") / "state.json").read_text())
print("\n--- eventi del router ---")
for t, m in router.events:
    print(m)
print("\nvalore finale:", final)
assert final["value"] == 6, "la catena non è completa!"
assert any("handoff_queued" in m for _, m in router.events), "hand-off non attivato"
assert any("OFFLINE" in m for _, m in router.events), "watchdog non ha rilevato il crash"
print("OK: hand-off anticipato + recupero dal crash verificati")
shutil.rmtree(root, ignore_errors=True)
