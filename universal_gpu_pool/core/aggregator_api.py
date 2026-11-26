"""Router centrale: API REST + macchina a stati + pagina di stato. Tutte le transizioni di
stato sono codice deterministico sotto un unico lock."""
import json, threading, time, urllib.request, uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from core.state_store import open_store
from core.ai_orchestrator import Planner

ACTIVE = ("running", "handoff_queued")
# Preferisci la webapp dedicata; fallback alla status page classica
_WEBAPP = Path(__file__).resolve().parent.parent / "webapp" / "index.html"
_LEGACY = Path(__file__).with_name("status_page.html")
PAGE = _WEBAPP if _WEBAPP.exists() else _LEGACY


class Router:
    def __init__(self, cfg, store=None):
        self.cfg = cfg
        self.store = store or open_store(cfg)
        self.planner = Planner(cfg)
        self.lock = threading.RLock()
        self.workers = {}   # id -> {profile, name, last_seen, status, session_start}
        self.jobs = {}      # id -> {manifest, steps:{id:state}, order:[ids], status}
        self.events = []
        self._recover()

    def log(self, msg):
        self.events.append((round(time.time(), 2), msg))
        self.events = self.events[-300:]

    def _notify(self, msg):
        url = self.cfg.get("notify_url")      # es. https://ntfy.sh/il-tuo-topic-segreto
        if not url:
            return

        def send():
            try:
                urllib.request.urlopen(urllib.request.Request(
                    url, data=msg.encode(), method="POST", headers={"Title": "GPU Pool"}), timeout=10)
            except Exception:
                pass
        threading.Thread(target=send, daemon=True).start()

    # ---------- persistenza ----------
    def _persist_step(self, job_id, st):
        self.store.put_json(f"jobs/{job_id}/state/{st['id']}.state.json", st)

    def _persist_cluster(self):
        self.store.put_json("cluster_state.json", {
            "workers": self.workers,
            "jobs": {j: {"status": v["status"],
                         "done": sum(s["status"] == "completed" for s in v["steps"].values()),
                         "total": len(v["steps"])} for j, v in self.jobs.items()}})

    def _recover(self):
        for jid in self.store.list_job_ids():
            m = self.store.get_json(f"jobs/{jid}/manifest.json")
            if not m or "steps" not in m:
                continue
            steps = {}
            for s in m["steps"]:
                st = self.store.get_json(f"jobs/{jid}/state/{s['id']}.state.json") or self._new_state(jid, s)
                if st["status"] in ACTIVE:           # i worker sono sconosciuti dopo il riavvio
                    st.update(status="pending", worker=None)
                    self._persist_step(jid, st)
                steps[s["id"]] = st
            statuses = [s["status"] for s in steps.values()]
            if statuses and all(s == "completed" for s in statuses):
                job_status = "completed"
            elif statuses and all(s in ("completed", "failed") for s in statuses) and any(
                s == "failed" for s in statuses
            ):
                job_status = "failed"
            else:
                job_status = "running"
            self.jobs[jid] = {
                "manifest": m,
                "steps": steps,
                "order": [s["id"] for s in m["steps"]],
                "status": job_status,
            }

    @staticmethod
    def _new_state(job_id, s):
        return {"id": s["id"], "job_id": job_id, "status": "pending", "worker": None,
                "depends_on": s.get("depends_on", []), "attempts": 0, "lock_ts": None,
                "handoff_triggered": False, "progress": 0.0}

    # ---------- job ----------
    def submit(self, manifest):
        with self.lock:
            jid = manifest["job_id"] = manifest.get("job_id") or "job_" + uuid.uuid4().hex[:6]
            self.store.put_json(f"jobs/{jid}/manifest.json", manifest)
            steps = {s["id"]: self._new_state(jid, s) for s in manifest["steps"]}
            for st in steps.values():
                self._persist_step(jid, st)
            self.jobs[jid] = {"manifest": manifest, "steps": steps,
                              "order": [s["id"] for s in manifest["steps"]], "status": "running"}
            self.log(f"job {jid} sottomesso ({len(steps)} step)")
            self._persist_cluster()
            return jid

    def plan_and_submit(self, body):
        from adapters import get_adapter
        adapter = get_adapter(body["adapter"])
        vram = [w["profile"].get("vram_gb", 0) for w in self.workers.values() if w["status"] == "online"]
        min_vram = min(vram) if vram else self.cfg.get("default_vram_gb", 14)
        params = body.get("params", {})
        steps = adapter().plan(body.get("items", []), params, {"min_vram_gb": min_vram, "planner": self.planner})
        return self.submit({"job_id": body.get("job_id"), "adapter": body["adapter"],
                            "params": params, "steps": steps})

    def _deps_done(self, job, st):
        return all(job["steps"][d]["status"] == "completed" for d in st["depends_on"])

    # ---------- worker ----------
    def register(self, profile):
        with self.lock:
            wid = "w_" + uuid.uuid4().hex[:5]
            self.workers[wid] = {"profile": profile, "name": profile.get("name", wid), "last_seen": time.time(),
                                 "status": "online", "session_start": time.time()}
            self.log(f"{wid} ({self.workers[wid]['name']}) registrato: {profile.get('gpu', '?')}, "
                     f"{profile.get('vram_gb', 0):.0f} GB")
            self._persist_cluster()
            return wid

    def heartbeat(self, body):
        with self.lock:
            w = self.workers.get(body["worker_id"])
            if not w or w["status"] != "online":
                return False
            w["last_seen"] = time.time()
            w["vram_free_gb"] = body.get("vram_free_gb")
            if body.get("vram_total_gb"):
                self.planner.note_pressure(body["worker_id"], body["vram_total_gb"] - (body.get("vram_free_gb") or 0),
                                           body["vram_total_gb"])
            jid, sid = body.get("job_id"), body.get("step_id")
            if jid in self.jobs and sid in self.jobs[jid]["steps"]:
                st = self.jobs[jid]["steps"][sid]
                if st["status"] == "running" and st["worker"] == body["worker_id"]:
                    st["progress"] = body.get("progress", 0.0)
                    age = (time.time() - w["session_start"]) / self.cfg["session_limit_s"]
                    thr = self.cfg["handoff_threshold"]
                    if not st["handoff_triggered"] and (st["progress"] >= thr or age >= thr):
                        self._trigger_handoff(self.jobs[jid], st)
            return True

    def _trigger_handoff(self, job, st):
        st["handoff_triggered"] = True
        idx = job["order"].index(st["id"])
        for nid in job["order"][idx + 1:]:
            nxt = job["steps"][nid]
            if nxt["status"] == "pending" and not self._deps_done(job, nxt):
                nxt["status"] = "handoff_queued"
                self._persist_step(job["manifest"]["job_id"], nxt)
                self.log(f"hand-off: {nid} -> handoff_queued (trigger da {st['id']} @ {st['progress']:.0%})")
                return

    def next_task(self, worker_id):
        with self.lock:
            w = self.workers.get(worker_id)
            if not w or w["status"] != "online":
                return None
            w["last_seen"] = time.time()
            # Prima i task pronti all'esecuzione; ignora job già terminati
            for job in self.jobs.values():
                if job["status"] in ("completed", "failed"):
                    continue
                for sid in job["order"]:
                    st = job["steps"][sid]
                    if st["status"] == "pending" and self._deps_done(job, st):
                        return self._assign(job, st, worker_id, "run")
            # Poi prefetch (hand-off): non è migrazione di stato del modello, solo precaricamento
            for job in self.jobs.values():
                if job["status"] in ("completed", "failed"):
                    continue
                for sid in job["order"]:
                    st = job["steps"][sid]
                    if st["status"] == "handoff_queued" and st["worker"] is None:
                        return self._assign(job, st, worker_id, "prefetch")
            return None

    def _assign(self, job, st, worker_id, mode):
        st["worker"], st["lock_ts"] = worker_id, time.time()
        if mode == "run":
            st["status"] = "running"
        self._persist_step(job["manifest"]["job_id"], st)
        self.log(f"{st['id']} -> {self.workers[worker_id]['name']} ({mode})")
        m = job["manifest"]
        step = next(s for s in m["steps"] if s["id"] == st["id"])
        return {"mode": mode, "job_id": m["job_id"], "adapter": m["adapter"],
                "params": m.get("params", {}), "step": step}

    def ready(self, worker_id, job_id, step_id):
        with self.lock:
            job = self.jobs.get(job_id)
            st = job["steps"].get(step_id) if job else None
            if not st or st["worker"] != worker_id:
                return {"ready": False, "abort": True}
            if st["status"] == "running":
                return {"ready": True}
            if st["status"] == "handoff_queued" and self._deps_done(job, st):
                st["status"], st["lock_ts"] = "running", time.time()
                self._persist_step(job_id, st)
                self.log(f"{step_id} promosso a running (prefetch completato)")
                return {"ready": True}
            return {"ready": False}

    def commit(self, body):
        with self.lock:
            job = self.jobs.get(body["job_id"])
            st = job["steps"].get(body["step_id"]) if job else None
            if not st:
                return False, "commit scartato: step sconosciuto"
            # Idempotenza: stesso worker che ha già completato  ok (no doppia esecuzione logica)
            if st["status"] == "completed" and st.get("worker") == body["worker_id"]:
                return True, "già completato"
            if st["status"] == "completed":
                return False, "commit scartato: step già completato da altro worker"
            if st["status"] != "running" or st["worker"] != body["worker_id"]:
                return False, "commit scartato: lock non più valido"
            if not self.store.finalize_output(body["job_id"], body["step_id"], body["worker_id"], body["sha256"]):
                return False, "verifica output fallita"
            st.update(status="completed", progress=1.0, metrics=body.get("metrics", {}))
            self._persist_step(body["job_id"], st)
            self.log(f"{st['id']} completed")
            if all(s["status"] == "completed" for s in job["steps"].values()):
                job["status"] = "completed"
                self.log(f"job {body['job_id']} COMPLETATO")
                self._notify(f"Job {body['job_id']} completato ({len(job['steps'])} step)")
            self._persist_cluster()
            return True, "ok"

    def fail(self, body):
        with self.lock:
            st = self.jobs[body["job_id"]]["steps"][body["step_id"]]
            if st["worker"] == body["worker_id"]:
                self._requeue(st, f"errore: {body.get('error')}")

    # ---------- watchdog ----------
    def _requeue(self, st, why):
        st["attempts"] += 1
        failed = st["attempts"] > self.cfg["max_attempts"]
        st.update(worker=None, lock_ts=None, handoff_triggered=False, status="failed" if failed else "pending")
        self._persist_step(st["job_id"], st)
        self.log(f"{st['id']} rimesso in coda ({why}), tentativo {st['attempts']}")
        if failed:
            job = self.jobs.get(st["job_id"])
            if job and job["status"] != "completed":
                # job fallito se ogni step è completed o failed (nessuno ancora eseguibile)
                terminal = all(s["status"] in ("completed", "failed") for s in job["steps"].values())
                any_fail = any(s["status"] == "failed" for s in job["steps"].values())
                if terminal and any_fail:
                    job["status"] = "failed"
                    self.log(f"job {st['job_id']} FALLITO")
                    self._persist_cluster()
            self._notify(f"Step {st['id']} del job {st['job_id']} FALLITO dopo {st['attempts']} tentativi")

    def reap(self):
        with self.lock:
            now = time.time()
            for wid, w in self.workers.items():
                if w["status"] == "online" and now - w["last_seen"] > self.cfg["lease_timeout"]:
                    w["status"] = "offline"
                    self.log(f"{w['name']} OFFLINE (lease scaduto)")
                    for job in self.jobs.values():
                        for st in job["steps"].values():
                            if st["worker"] == wid and st["status"] in ACTIVE:
                                self._requeue(st, f"worker {w['name']} caduto")
                    self._persist_cluster()

    # ---------- stato / domanda di nodi ----------
    def demand(self):
        """Quanti nodi GPU esterni servono.

        I job con runtime remote_api sono esclusi dal conteggio GPU: li gestisce il worker
        locale (stesso host del router) senza Kaggle/RunPod.
        """
        with self.lock:
            runnable = waiting = 0
            for job in self.jobs.values():
                if job["status"] in ("completed", "failed"):
                    continue
                params = job["manifest"].get("params") or {}
                runtime = params.get("runtime") or (params.get("options") or {}).get("runtime")
                # path free API: non richiede nodi GPU del launcher
                if runtime == "remote_api" or (
                    job["manifest"].get("adapter") == "universal_model"
                    and not params.get("model_ref")
                    and params.get("runtime", "remote_api") == "remote_api"
                ):
                    continue
                for st in job["steps"].values():
                    if st["status"] == "running" or (st["status"] == "pending" and self._deps_done(job, st)):
                        runnable += 1
                    elif st["status"] in ("pending", "handoff_queued"):
                        waiting += 1
            now, thr = time.time(), self.cfg["handoff_threshold"]
            expiring = sum(
                1 for w in self.workers.values()
                if w["status"] == "online"
                and w.get("name") != "local-api"
                and (now - w["session_start"]) / self.cfg["session_limit_s"] >= thr
            )
            need = runnable + (1 if (waiting and runnable) else 0) + (expiring if runnable else 0)
            return {"runnable": runnable, "waiting": waiting, "expiring": expiring, "nodes_needed": need}

    def status_all(self):
        with self.lock:
            now = time.time()
            jobs = []
            for jid, j in self.jobs.items():
                c = Counter(s["status"] for s in j["steps"].values())
                jobs.append({"id": jid, "status": j["status"], "adapter": j["manifest"].get("adapter"),
                             "total": len(j["steps"]), "done": c["completed"], "running": c["running"],
                             "failed": c["failed"]})
            workers = [{"id": i, "name": w["name"], "gpu": w["profile"].get("gpu"),
                        "vram_gb": round(w["profile"].get("vram_gb", 0), 1), "status": w["status"],
                        "age_s": int(now - w["session_start"]), "silent_s": int(now - w["last_seen"])}
                       for i, w in self.workers.items()]
            try:
                launcher = self.store.get_json("launcher_state.json", {})
            except Exception:
                launcher = {}
            return {"now": now, "jobs": jobs, "workers": workers, "demand": self.demand(), "launcher": launcher,
                    "advice": self.planner.advice[-5:],
                    "events": [{"t": t, "m": m} for t, m in self.events[-30:]]}

    # ---------- dispatch HTTP ----------
    def handle(self, method, path, q, body):
        if (method, path) == ("GET", "/status"):
            return 200, self.status_all()
        if (method, path) == ("POST", "/workers/register"):
            return 200, {"worker_id": self.register(body)}
        if (method, path) == ("POST", "/workers/heartbeat"):
            return (200, {"ok": True}) if self.heartbeat(body) else (404, {"ok": False})
        if (method, path) == ("GET", "/tasks/next"):
            return 200, {"task": self.next_task(q["worker_id"])}
        if (method, path) == ("GET", "/tasks/ready"):
            return 200, self.ready(q["worker_id"], q["job_id"], q["step_id"])
        if (method, path) == ("POST", "/tasks/commit"):
            ok, msg = self.commit(body)
            return (200 if ok else 409), {"ok": ok, "msg": msg}
        if (method, path) == ("POST", "/tasks/fail"):
            self.fail(body)
            return 200, {"ok": True}
        if (method, path) == ("POST", "/jobs"):
            return 200, {"job_id": self.submit(body)}
        if (method, path) == ("POST", "/jobs/plan"):
            return 200, {"job_id": self.plan_and_submit(body)}
        if method == "GET" and path.startswith("/jobs/"):
            parts = [p for p in path.split("/") if p]
            # /jobs/{id} oppure /jobs/{id}/results
            if len(parts) < 2:
                return 404, {"error": "job sconosciuto"}
            jid = parts[1]
            j = self.jobs.get(jid)
            if not j:
                return 404, {"error": "job sconosciuto"}
            if len(parts) >= 3 and parts[2] == "results":
                return 200, self.job_results(jid)
            return 200, {"status": j["status"], "steps": {s: v["status"] for s, v in j["steps"].items()},
                         "adapter": j["manifest"].get("adapter"), "params": j["manifest"].get("params", {})}
        return 404, {"error": "not found"}

    def job_results(self, job_id):
        """Legge gli output committati (JSONL results.jsonl o file di testo) per la webapp."""
        with self.lock:
            j = self.jobs.get(job_id)
            if not j:
                return {"error": "job sconosciuto", "items": []}
            items, errors = [], []
            for sid in j["order"]:
                st = j["steps"][sid]
                if st["status"] != "completed":
                    continue
                try:
                    out_dir = self.store.fetch_output(job_id, sid)
                except Exception as e:
                    errors.append(f"{sid}: {e}")
                    continue
                # universal_model e simili  results.jsonl
                jl = out_dir / "results.jsonl"
                if jl.exists():
                    for line in jl.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            rec = {"raw": line}
                        rec["_step"] = sid
                        items.append(rec)
                    continue
                # fallback: qualsiasi .json / .txt piccolo
                for p in sorted(out_dir.rglob("*")):
                    if not p.is_file() or p.name.startswith("_"):
                        continue
                    if p.suffix.lower() in (".json", ".jsonl", ".txt", ".md") and p.stat().st_size < 2_000_000:
                        try:
                            text = p.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            continue
                        items.append({"_step": sid, "file": p.name, "text": text[:50000]})
            return {
                "job_id": job_id,
                "status": j["status"],
                "adapter": j["manifest"].get("adapter"),
                "items": items,
                "errors": errors,
            }


def make_server(router, host, port):
    token = router.cfg["token"]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _go(self, method):
            u = urlparse(self.path)
            if method == "GET" and u.path in ("/", "/ui"):      # pagina statica: i dati richiedono il token
                return self._send_raw(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if self.headers.get("X-Pool-Token") != token:
                return self._send(401, {"error": "token non valido"})
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            try:
                code, obj = router.handle(method, u.path, q, body)
            except Exception as e:
                code, obj = 500, {"error": repr(e)}
            self._send(code, obj)

        def _send_raw(self, code, data, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send(self, code, obj):
            self._send_raw(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self): self._go("GET")
        def do_POST(self): self._go("POST")

    return ThreadingHTTPServer((host, port), H)
