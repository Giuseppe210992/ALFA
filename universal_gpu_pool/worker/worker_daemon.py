"""Daemon del nodo GPU: registra, heartbeat, prefetch, esecuzione, scrittura atomica, commit."""
import threading, time, traceback
from adapters import get_adapter
from adapters.base_adapter import StepContext
from pipelines.dag_runner import PoolClient
from worker import telemetry, environment_manager as env


class Crashed(Exception):
    pass


class Worker:
    def __init__(self, url, token, store, name="node", hb=10.0, poll=2.0, session_limit_s=32400,
                 crash_at=None, verbose=True):
        self.client, self.store, self.name = PoolClient(url, token), store, name
        self.hb, self.poll, self.limit, self.verbose = hb, poll, session_limit_s, verbose
        self.crash_at = crash_at          # (step_id, progress) per simulare un kill improvviso
        self.dead = False
        self.cur = {"job_id": None, "step_id": None, "progress": 0.0}
        self.t0 = time.time()
        self.cache = env.local_cache(f"/tmp/gpu_pool_cache_{name}")

    def say(self, m):
        if self.verbose:
            print(f"[{self.name}] {m}", flush=True)

    def _hb_loop(self):
        while not self.dead and not self.stop_hb:
            gpu, total, free = telemetry.gpu_info()
            try:
                r = self.client.call("POST", "/workers/heartbeat", {
                    "worker_id": self.wid, "vram_free_gb": free, "vram_total_gb": total, **self.cur})
            except Exception:
                r = {}
            time.sleep(self.hb)

    def run(self, max_idle_s=None):
        gpu, total, free = telemetry.gpu_info()
        self.wid = self.client.call("POST", "/workers/register", {
            "gpu": gpu, "vram_gb": total, "name": self.name})["worker_id"]
        self.say(f"registrato come {self.wid} ({gpu}, {total:.0f} GB)")
        self.stop_hb = False
        threading.Thread(target=self._hb_loop, daemon=True).start()
        idle0 = time.time()
        try:
            while not self.dead:
                if time.time() - self.t0 > self.limit * 0.9:
                    self.say("sessione quasi al limite: non accetto nuovi task, esco"); break
                task = self.client.call("GET", f"/tasks/next?worker_id={self.wid}")["task"]
                if not task:
                    if max_idle_s and time.time() - idle0 > max_idle_s:
                        break
                    time.sleep(self.poll); continue
                idle0 = time.time()
                self._handle(task)
        except Crashed:
            self.say("*** CRASH SIMULATO: il nodo sparisce senza preavviso ***")
        finally:
            self.stop_hb = True

    def _handle(self, task):
        jid, step = task["job_id"], task["step"]
        sid = step["id"]
        adapter = get_adapter(task["adapter"])()
        tmp = self.store.tmp_dir(jid, sid, self.wid)
        self.cur = {"job_id": jid, "step_id": sid, "progress": 0.0}

        def progress(p):
            self.cur["progress"] = p
            if self.crash_at and self.crash_at[0] in ("*", sid) and p >= self.crash_at[1]:
                self.dead = True
                raise Crashed()
        ctx = StepContext(self.store, jid, step, task["params"], tmp, progress, self.name, self.cache)
        try:
            if task["mode"] == "prefetch":
                self.say(f"prefetch {sid}")
                adapter.prefetch(ctx)
                while True:
                    r = self.client.call("GET", f"/tasks/ready?worker_id={self.wid}&job_id={jid}&step_id={sid}")
                    if r.get("abort"):
                        self.say(f"prefetch {sid} annullato"); return
                    if r["ready"]:
                        break
                    time.sleep(min(self.poll, 0.2))
            self.say(f"esecuzione {sid}")
            adapter.run_step(ctx)
            if not adapter.validate(ctx):
                raise RuntimeError("validazione fallita (output vuoto/corrotto/loss non finita)")
            digest = self.store.stage_output(tmp, jid, sid, self.wid)   # carica + digest; promuove il ROUTER
            r = self.client.call("POST", "/tasks/commit", {"worker_id": self.wid, "job_id": jid, "step_id": sid,
                                                           "sha256": digest, "metrics": {}})
            self.say(f"commit {sid}: {r.get('msg')}")
        except Crashed:
            raise
        except Exception as e:
            traceback.print_exc()
            self.client.call("POST", "/tasks/fail", {"worker_id": self.wid, "job_id": jid, "step_id": sid,
                                                     "error": repr(e)})
        finally:
            self.cur = {"job_id": None, "step_id": None, "progress": 0.0}
            env.clear_cuda_cache()
