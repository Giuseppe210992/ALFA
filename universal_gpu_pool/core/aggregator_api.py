"""Router centrale: API REST + macchina a stati + pagina di stato. Tutte le transizioni di
stato sono codice deterministico sotto un unico lock."""
import hmac, json, threading, time, urllib.request, uuid
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


class BadRequest(Exception):
    """Richiesta non valida: diventa un 400 con messaggio leggibile."""


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
        if not isinstance(manifest, dict) or not manifest.get("adapter"):
            raise BadRequest("manca 'adapter'")
        steps_in = manifest.get("steps")
        if not isinstance(steps_in, list) or not steps_in:
            raise BadRequest("il job non ha step: servono almeno un elemento/prompt")
        ids = [s.get("id") for s in steps_in if isinstance(s, dict)]
        if len(ids) != len(steps_in) or None in ids or len(set(ids)) != len(ids):
            raise BadRequest("ogni step deve avere un 'id' univoco")
        for s in steps_in:
            for d in s.get("depends_on", []):
                if d not in ids:
                    raise BadRequest(f"lo step {s['id']} dipende da uno step inesistente: {d}")
        with self.lock:
            if manifest.get("job_id") and manifest["job_id"] in self.jobs:
                raise BadRequest(f"job_id già esistente: {manifest['job_id']}")
            jid = manifest["job_id"] = manifest.get("job_id") or "job_" + uuid.uuid4().hex[:6]
            manifest.setdefault("created", time.time())
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
        if not isinstance(body, dict) or not body.get("adapter"):
            raise BadRequest("manca 'adapter'")
        try:
            adapter = get_adapter(body["adapter"])
        except KeyError as e:
            raise BadRequest(str(e).strip("'\""))
        if not isinstance(body.get("items", []), list):
            raise BadRequest("'items' deve essere una lista")
        vram = [w["profile"].get("vram_gb", 0) for w in self.workers.values()
                if w["status"] == "online" and not w["profile"].get("api_only")]
        min_vram = min(vram) if vram else self.cfg.get("default_vram_gb", 14)
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise BadRequest("'params' deve essere un oggetto JSON")
        try:
            steps = adapter().plan(body.get("items", []), params, {"min_vram_gb": min_vram, "planner": self.planner})
        except (ValueError, TypeError, KeyError) as e:
            raise BadRequest(f"parametri non validi per {body['adapter']}: {e}")
        return self.submit({"job_id": body.get("job_id"), "adapter": body["adapter"],
                            "params": params, "steps": steps})

    def _refresh_job_status(self, job):
        """completed se tutti gli step sono completed; failed se tutti terminati e almeno uno fallito."""
        if job["status"] in ("completed", "failed"):
            return
        # step in attesa di una dipendenza fallita non partiranno mai: falliscono anche loro
        changed = True
        while changed:
            changed = False
            for st in job["steps"].values():
                if st["status"] in ("pending", "handoff_queued") and any(
                        job["steps"][d]["status"] == "failed" for d in st["depends_on"]):
                    st.update(status="failed", worker=None, error="dipendenza fallita")
                    self._persist_step(job["manifest"]["job_id"], st)
                    changed = True
        sts = [s["status"] for s in job["steps"].values()]
        if sts and all(x == "completed" for x in sts):
            job["status"] = "completed"
            self.log(f"job {job['manifest']['job_id']} COMPLETATO")
            self._notify(f"Job {job['manifest']['job_id']} completato ({len(sts)} step)")
        elif sts and all(x in ("completed", "failed") for x in sts):
            job["status"] = "failed"
            self.log(f"job {job['manifest']['job_id']} FALLITO")
            self._notify(f"Job {job['manifest']['job_id']} FALLITO")

    @staticmethod
    def is_api_job(job):
        """Job che non richiede GPU: universal_model con runtime remote_api (path gratuito)."""
        m = job["manifest"]
        params = m.get("params") or {}
        runtime = params.get("runtime") or (params.get("options") or {}).get("runtime")
        if runtime == "remote_api":
            return True
        return m.get("adapter") == "universal_model" and not params.get("model_ref") and not runtime

    def _can_take(self, worker, job):
        """Il worker solo-API (questo computer, senza GPU) prende solo job API;
        i nodi GPU lasciano i job API al worker solo-API quando ce n'è uno online."""
        api_job = self.is_api_job(job)
        if worker["profile"].get("api_only"):
            return api_job
        if api_job:
            return not any(w["status"] == "online" and w["profile"].get("api_only") for w in self.workers.values())
        return True

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
                if job["status"] in ("completed", "failed") or not self._can_take(w, job):
                    continue
                for sid in job["order"]:
                    st = job["steps"][sid]
                    if st["status"] == "pending" and self._deps_done(job, st):
                        return self._assign(job, st, worker_id, "run")
            # Poi prefetch (hand-off): non è migrazione di stato del modello, solo precaricamento
            for job in self.jobs.values():
                if job["status"] in ("completed", "failed") or not self._can_take(w, job):
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
            self._refresh_job_status(job)
            self._persist_cluster()
            return True, "ok"

    def fail(self, body):
        with self.lock:
            job = self.jobs.get(body.get("job_id"))
            st = job["steps"].get(body.get("step_id")) if job else None
            if st and st["worker"] == body.get("worker_id") and st["status"] in ACTIVE:
                self._requeue(st, f"errore: {body.get('error')}")

    # ---------- watchdog ----------
    def _requeue(self, st, why):
        st["attempts"] += 1
        failed = st["attempts"] > self.cfg["max_attempts"]
        st.update(worker=None, lock_ts=None, handoff_triggered=False, status="failed" if failed else "pending",
                  error=why)
        self._persist_step(st["job_id"], st)
        self.log(f"{st['id']} rimesso in coda ({why}), tentativo {st['attempts']}")
        if failed:
            job = self.jobs.get(st["job_id"])
            if job:
                self._refresh_job_status(job)
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
                # path free API: non richiede nodi GPU del launcher
                if self.is_api_job(job):
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
                and not w["profile"].get("api_only")
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
                ms = [s.get("metrics") or {} for s in j["steps"].values()]
                prog = sum(1.0 if s["status"] == "completed" else float(s.get("progress") or 0)
                           if s["status"] == "running" else 0.0 for s in j["steps"].values())
                params = j["manifest"].get("params") or {}
                jobs.append({"id": jid, "status": j["status"], "adapter": j["manifest"].get("adapter"),
                             "runtime": params.get("runtime"), "title": params.get("title"),
                             "created": j["manifest"].get("created"),
                             "total": len(j["steps"]), "done": c["completed"], "running": c["running"],
                             "failed": c["failed"],
                             "progress": round(prog / max(1, len(j["steps"])), 3),
                             "items_ok": sum(int(m.get("ok", 0)) for m in ms),
                             "items_failed": sum(int(m.get("failed", 0)) for m in ms)})
            jobs.sort(key=lambda x: x.get("created") or 0)
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

    # ---------- account / provider API (webapp) ----------
    def _backend(self):
        from core.remote_api_backend import get_shared_backend
        return get_shared_backend()

    def settings(self):
        import os
        from core.remote_api_backend import load_providers
        from core.secrets_store import hint
        try:
            st = self._backend().status()
        except Exception:
            st = {}
        provs = []
        for p in load_providers():
            d = {k: getattr(p, k) for k in ("name", "label", "base_url", "model", "key_env", "rpm", "rpd",
                                            "enabled", "signup_url", "notes")}
            d["has_key"] = p.has_key()
            d["key_hint"] = hint(os.environ.get(p.key_env, ""))
            d["status"] = st.get(p.name, {})
            provs.append(d)
        return {"providers": provs,
                "ready": any(p["has_key"] and p["enabled"] and p["base_url"] for p in provs)}

    def save_providers(self, body):
        from core.remote_api_backend import Provider, load_providers, save_providers, reset_shared_backend
        items = body.get("providers") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items:
            raise BadRequest("serve una lista 'providers'")
        current = {p.name: p for p in load_providers()}
        out, seen = [], set()
        for d in items:
            if not isinstance(d, dict) or not d.get("name"):
                raise BadRequest("ogni provider deve avere un 'name'")
            name = str(d["name"]).strip()
            if name in seen:
                raise BadRequest(f"provider duplicato: {name}")
            seen.add(name)
            base = current.get(name)
            merged = {**(vars(base) if base else {}), **d}
            from core.secrets_store import valid_name
            if not valid_name(merged.get("key_env", "")):
                raise BadRequest(f"{name}: nome variabile chiave non valido")
            url = str(merged.get("base_url") or "").strip()
            if url and not url.startswith(("https://", "http://")):
                raise BadRequest(f"{name}: l'URL deve iniziare con https://")
            url = url.rstrip("/")
            if url.endswith("/chat/completions"):
                url = url[:-len("/chat/completions")]
            merged["base_url"] = url
            merged["model"] = str(merged.get("model") or "").strip()
            try:
                out.append(Provider.from_dict(merged))
            except (TypeError, ValueError) as e:
                raise BadRequest(f"{name}: {e}")
        save_providers(out)
        reset_shared_backend()
        self.log("configurazione provider aggiornata")
        return self.settings()

    def reset_providers(self):
        from core.remote_api_backend import default_providers, save_providers, reset_shared_backend
        save_providers(default_providers())
        reset_shared_backend()
        self.log("provider ripristinati ai valori predefiniti")
        return self.settings()

    def set_key(self, body):
        from core.remote_api_backend import load_providers, reset_shared_backend
        from core.secrets_store import set_secret
        name = (body or {}).get("key_env", "")
        allowed = {p.key_env for p in load_providers()}
        if name not in allowed:
            raise BadRequest(f"variabile non gestita: {name}")
        try:
            set_secret(name, body.get("value", ""))
        except ValueError as e:
            raise BadRequest(str(e))
        reset_shared_backend()
        self.log(f"chiave {name} {'salvata' if body.get('value') else 'rimossa'}")
        return self.settings()

    def test_provider(self, body):
        name = (body or {}).get("name")
        if not name:
            raise BadRequest("manca 'name'")
        return self._backend().test_provider(name)

    def cancel_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            for st in job["steps"].values():
                if st["status"] != "completed":
                    st.update(status="failed", worker=None, error="annullato dall'utente")
                    self._persist_step(job_id, st)
            if job["status"] not in ("completed",):
                job["status"] = "failed"
            self.log(f"job {job_id} annullato")
            self._persist_cluster()
            return True

    def retry_job(self, job_id):
        """Rilancia un job: stessi parametri, solo gli elementi falliti o mancanti."""
        res = self.job_results(job_id)
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise BadRequest("job sconosciuto")
            m = job["manifest"]
        ok_ids = {str(r.get("id")) for r in res["items"] if r.get("ok") and r.get("id") is not None}
        items = []
        for s in m["steps"]:
            for it in (s.get("payload") or {}).get("items", []):
                if isinstance(it, dict) and str(it.get("id")) not in ok_ids:
                    items.append(it)
        if not items:
            raise BadRequest("niente da rilanciare: tutti gli elementi sono già riusciti "
                             "(oppure questo tipo di job non è rilanciabile)")
        return self.plan_and_submit({"adapter": m["adapter"], "items": items, "params": m.get("params", {})})

    # ---------- dispatch HTTP ----------
    @staticmethod
    def _q(q, *names):
        missing = [n for n in names if not q.get(n)]
        if missing:
            raise BadRequest(f"parametri mancanti: {', '.join(missing)}")
        return [q[n] for n in names]

    def handle(self, method, path, q, body):
        if (method, path) == ("GET", "/status"):
            return 200, self.status_all()
        if (method, path) == ("GET", "/health"):
            return 200, {"ok": True}
        if (method, path) == ("POST", "/workers/register"):
            return 200, {"worker_id": self.register(body)}
        if (method, path) == ("POST", "/workers/heartbeat"):
            return (200, {"ok": True}) if self.heartbeat(body) else (404, {"ok": False})
        if (method, path) == ("GET", "/tasks/next"):
            return 200, {"task": self.next_task(*self._q(q, "worker_id"))}
        if (method, path) == ("GET", "/tasks/ready"):
            return 200, self.ready(*self._q(q, "worker_id", "job_id", "step_id"))
        if (method, path) == ("POST", "/tasks/commit"):
            for k in ("worker_id", "job_id", "step_id", "sha256"):
                if not body.get(k):
                    raise BadRequest(f"manca '{k}'")
            ok, msg = self.commit(body)
            return (200 if ok else 409), {"ok": ok, "msg": msg}
        if (method, path) == ("POST", "/tasks/fail"):
            self.fail(body)
            return 200, {"ok": True}
        if (method, path) == ("POST", "/jobs"):
            return 200, {"job_id": self.submit(body)}
        if (method, path) == ("POST", "/jobs/plan"):
            return 200, {"job_id": self.plan_and_submit(body)}
        if (method, path) == ("GET", "/settings"):
            return 200, self.settings()
        if (method, path) == ("POST", "/settings/providers"):
            return 200, self.save_providers(body)
        if (method, path) == ("POST", "/settings/providers/reset"):
            return 200, self.reset_providers()
        if (method, path) == ("POST", "/settings/key"):
            return 200, self.set_key(body)
        if (method, path) == ("POST", "/settings/test"):
            return 200, self.test_provider(body)
        if path.startswith("/jobs/"):
            parts = [p for p in path.split("/") if p]
            # /jobs/{id}, /jobs/{id}/results, /jobs/{id}/cancel, /jobs/{id}/retry
            if len(parts) < 2 or parts[1] not in self.jobs:
                return 404, {"error": "job sconosciuto"}
            jid = parts[1]
            action = parts[2] if len(parts) >= 3 else ""
            if method == "GET" and action == "results":
                return 200, self.job_results(jid)
            if method == "POST" and action == "cancel":
                return 200, {"ok": self.cancel_job(jid)}
            if method == "POST" and action == "retry":
                return 200, {"job_id": self.retry_job(jid)}
            if method == "GET" and not action:
                with self.lock:
                    j = self.jobs[jid]
                    return 200, {"status": j["status"],
                                 "steps": {s: v["status"] for s, v in j["steps"].items()},
                                 "adapter": j["manifest"].get("adapter"),
                                 "params": j["manifest"].get("params", {})}
        return 404, {"error": "not found"}

    def job_results(self, job_id):
        """Legge gli output committati (JSONL results.jsonl o file di testo) per la webapp.
        Lo stato si legge sotto lock; i file (anche su S3, lenti) si leggono fuori dal lock."""
        with self.lock:
            j = self.jobs.get(job_id)
            if not j:
                return {"error": "job sconosciuto", "items": []}
            done = [sid for sid in j["order"] if j["steps"][sid]["status"] == "completed"]
            status, adapter = j["status"], j["manifest"].get("adapter")
            step_errors = [f"{sid}: {st.get('error')}" for sid, st in j["steps"].items()
                           if st["status"] == "failed" and st.get("error")]
        items, errors = [], list(step_errors)
        for sid in done:
            try:
                out_dir = self.store.fetch_output(job_id, sid)
            except Exception as e:
                errors.append(f"{sid}: {e}")
                continue
            # universal_model e simili -> results.jsonl
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
                if not p.is_file() or p.name.startswith("_") or p.name.startswith("."):
                    continue
                if p.suffix.lower() in (".json", ".jsonl", ".txt", ".md") and p.stat().st_size < 2_000_000:
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    items.append({"_step": sid, "file": p.name, "text": text[:50000]})

        def _key(r):
            try:
                return (0, int(r.get("id")))
            except (TypeError, ValueError):
                return (1, str(r.get("id", "")))
        if items and all("id" in r for r in items):
            items.sort(key=_key)
        return {"job_id": job_id, "status": status, "adapter": adapter, "items": items, "errors": errors,
                "ok": sum(1 for r in items if r.get("ok") is True),
                "failed": sum(1 for r in items if r.get("ok") is False)}


MAX_BODY = 20 * 1024 * 1024     # 20 MB: abbastanza per migliaia di righe di testo


def make_server(router, host, port):
    def token_ok(got):
        return bool(got) and hmac.compare_digest(str(got).encode(), str(router.cfg["token"]).encode())

    class H(BaseHTTPRequestHandler):
        server_version = "GPUPool/1.1"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _go(self, method):
            u = urlparse(self.path)
            if method == "GET" and u.path in ("/", "/ui", "/index.html"):   # pagina statica: i dati richiedono il token
                return self._send_raw(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and u.path == "/favicon.ico":
                return self._send_raw(204, b"", "image/x-icon")
            if not token_ok(self.headers.get("X-Pool-Token")):
                return self._send(401, {"error": "token non valido"})
            try:
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                body = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        raise BadRequest("richiesta troppo grande")
                    raw = self.rfile.read(n) if n else b""
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        raise BadRequest("JSON non valido")
                    if not isinstance(body, dict):
                        raise BadRequest("il corpo deve essere un oggetto JSON")
                code, obj = router.handle(method, u.path, q, body)
            except BadRequest as e:
                code, obj = 400, {"error": str(e)}
            except Exception as e:
                code, obj = 500, {"error": f"{type(e).__name__}: {e}"}
            self._send(code, obj)

        def _send_raw(self, code, data, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _send(self, code, obj):
            self._send_raw(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

        def do_GET(self): self._go("GET")
        def do_POST(self): self._go("POST")

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv
