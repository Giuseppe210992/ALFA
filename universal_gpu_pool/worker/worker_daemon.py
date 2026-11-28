"""Daemon del nodo GPU: registra, heartbeat, prefetch, esecuzione, scrittura atomica, commit."""
import os, tempfile, threading, time, traceback
from adapters import get_adapter
from adapters.base_adapter import StepContext
from pipelines.dag_runner import PoolClient, PoolHTTPError
from worker import telemetry, environment_manager as env


class Crashed(Exception):
    pass


class Worker:
    def __init__(self, url, token, store, name="node", hb=10.0, poll=2.0, session_limit_s=32400,
                 crash_at=None, verbose=True, api_only=False):
        self.client, self.store, self.name = PoolClient(url, token), store, name
        self.hb, self.poll, self.limit, self.verbose = hb, poll, session_limit_s, verbose
        self.api_only = api_only          # True: nessuna GPU, esegue solo job via API
        self.crash_at = crash_at          # (step_id, progress) per simulare un kill improvviso
        self.dead = False
        self.stop_hb = False
        self.lost = False                 # il router non ci riconosce più (es. riavviato)
        self.wid = None
        self.cur = {"job_id": None, "step_id": None, "progress": 0.0}
        self.t0 = time.time()
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        self.cache = env.local_cache(os.path.join(tempfile.gettempdir(), f"gpu_pool_cache_{safe}"))

    def say(self, m):
        if self.verbose:
            print(f"[{self.name}] {m}", flush=True)

    def _register(self):
        gpu, total, free = telemetry.gpu_info()
        self.wid = self.client.call("POST", "/workers/register", {
            "gpu": "solo API" if self.api_only else gpu, "vram_gb": 0 if self.api_only else total,
            "name": self.name, "api_only": self.api_only})["worker_id"]
        self.lost = False
        self.say(f"registrato come {self.wid} ({gpu}, {total:.0f} GB)")

    def _hb_loop(self):
        while not self.dead and not self.stop_hb:
            gpu, total, free = telemetry.gpu_info()
            try:
                self.client.call("POST", "/workers/heartbeat", {
                    "worker_id": self.wid, "vram_free_gb": free, "vram_total_gb": total, **self.cur})
            except PoolHTTPError as e:
                if e.code == 404:          # lease scaduto o router riavviato: ci si ri-registra
                    self.lost = True
            except Exception:
                pass
            time.sleep(self.hb)

    def _call_retry(self, method, path, body=None, tries=5):
        """Chiamata al router tollerante a brevi interruzioni di rete."""
        for i in range(tries):
            try:
                return self.client.call(method, path, body)
            except PoolHTTPError:
                raise
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(min(2 ** i, 10))

    def run(self, max_idle_s=None):
        self._register()
        self.stop_hb = False
        threading.Thread(target=self._hb_loop, daemon=True).start()
        idle0 = time.time()
        try:
            while not self.dead:
                if time.time() - self.t0 > self.limit * 0.9:
                    self.say("sessione quasi al limite: non accetto nuovi task, esco"); break
                try:
                    if self.lost and not self.cur["job_id"]:
                        self._register()
                    task = self.client.call("GET", f"/tasks/next?worker_id={self.wid}")["task"]
                except Crashed:
                    raise
                except Exception as e:
                    self.say(f"router non raggiungibile ({e}); riprovo")
                    time.sleep(max(self.poll, 2)); continue
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
        self.cur = {"job_id": jid, "step_id": sid, "progress": 0.0}

        def progress(p):
            self.cur["progress"] = p
            if self.crash_at and self.crash_at[0] in ("*", sid) and p >= self.crash_at[1]:
                self.dead = True
                raise Crashed()
        try:
            adapter = get_adapter(task["adapter"])()
            tmp = self.store.tmp_dir(jid, sid, self.wid)
            ctx = StepContext(self.store, jid, step, task["params"], tmp, progress, self.name, self.cache,
                              note=lambda text: self.cur.__setitem__("note", str(text)[:200]))
            if task["mode"] == "prefetch":
                self.say(f"prefetch {sid}")
                adapter.prefetch(ctx)
                while True:
                    r = self._call_retry("GET", f"/tasks/ready?worker_id={self.wid}&job_id={jid}&step_id={sid}")
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
            metrics = dict(getattr(adapter, "last_metrics", None) or {})
            try:
                r = self._call_retry("POST", "/tasks/commit", {"worker_id": self.wid, "job_id": jid,
                                                               "step_id": sid, "sha256": digest,
                                                               "metrics": metrics})
                self.say(f"commit {sid}: {r.get('msg')}")
            except PoolHTTPError as e:      # 409: commit rifiutato (lock perso / già fatto da altri)
                self.say(f"commit {sid} rifiutato: {e}")
        except Crashed:
            raise
        except Exception as e:
            traceback.print_exc()
            try:
                self._call_retry("POST", "/tasks/fail", {"worker_id": self.wid, "job_id": jid, "step_id": sid,
                                                         "error": f"{type(e).__name__}: {e}"[:500]})
            except Exception as e2:
                self.say(f"impossibile segnalare il fallimento: {e2}")
        finally:
            self.cur = {"job_id": None, "step_id": None, "progress": 0.0}
            env.clear_cuda_cache()
