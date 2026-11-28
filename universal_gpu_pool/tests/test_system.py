"""Test di sistema: router HTTP reale + worker locale reale + finto provider OpenAI.

Ripercorre ciò che fa la webapp: login col token, collegamento di un account,
pulsante «Prova», domanda, batch, errori, annulla/riprova, riavvio del router.
"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import remote_api_backend as rab, secrets_store  # noqa: E402
from core.aggregator_api import Router, make_server  # noqa: E402
from core.state_store import open_store  # noqa: E402
from core.watchdog import Watchdog  # noqa: E402
from worker.worker_daemon import Worker  # noqa: E402
from tests.fake_openai import FakeOpenAI  # noqa: E402

TOKEN = "test-token-123456"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(base, method, path, body=None, token=TOKEN, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, method=method, data=data,
                                 headers={"X-Pool-Token": token or "", "Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=20) as r:
            ct = r.headers.get("Content-Type", "")
            b = r.read()
            return r.status, (json.loads(b) if "json" in ct else b.decode())
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, b.decode(errors="replace")


class SystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env_backup = dict(os.environ)
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        # file di configurazione isolati in una cartella temporanea
        cls.orig = (rab.PROVIDERS_FILE, secrets_store.SECRETS_FILE)
        rab.PROVIDERS_FILE = d / "providers.json"
        secrets_store.SECRETS_FILE = d / "secrets.env"
        os.environ["GPU_POOL_API_CACHE"] = str(d / "apicache")
        for p in rab.DEFAULT_PROVIDERS:
            os.environ.pop(p.key_env, None)
        rab.reset_shared_backend()

        cls.fake = FakeOpenAI().start()
        cls.cfg = dict(token=TOKEN, store_root=str(d / "store"), lease_timeout=6, handoff_threshold=0.7,
                       session_limit_s=3600, max_attempts=1, default_vram_gb=14, heartbeat_interval=1)
        cls.start_router()

    @classmethod
    def start_router(cls):
        cls.store = open_store(cls.cfg)
        cls.router = Router(cls.cfg, cls.store)
        cls.srv = make_server(cls.router, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.wd = Watchdog(cls.router, 0.5)
        cls.wd.start()
        cls.worker = Worker(cls.base, TOKEN, cls.store, name="local-api", api_only=True, hb=1, poll=0.2, verbose=False)
        threading.Thread(target=cls.worker.run, daemon=True).start()
        time.sleep(0.5)

    @classmethod
    def stop_router(cls):
        cls.worker.dead = True
        cls.wd.stop()
        cls.srv.shutdown()
        cls.srv.server_close()

    @classmethod
    def tearDownClass(cls):
        cls.stop_router()
        cls.fake.stop()
        rab.PROVIDERS_FILE, secrets_store.SECRETS_FILE = cls.orig
        os.environ.clear()
        os.environ.update(cls.env_backup)
        rab.reset_shared_backend()
        cls.tmp.cleanup()

    # ---------- helper ----------
    def call(self, method, path, body=None, **kw):
        return http(self.base, method, path, body, **kw)

    def wait_job(self, jid, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            code, s = self.call("GET", f"/jobs/{jid}")
            self.assertEqual(code, 200, s)
            if s["status"] in ("completed", "failed"):
                return s
            time.sleep(0.2)
        self.fail(f"job {jid} non terminato: {s}")

    def connect_account(self, key="good"):
        code, s = self.call("GET", "/settings")
        provs = s["providers"]
        for p in provs:
            if p["name"] == "custom":
                p["base_url"] = self.fake.base_url
                p["model"] = "fake-model"
                p["rpm"] = 1000
        clean = [{k: v for k, v in p.items() if k not in ("has_key", "key_hint", "status")} for p in provs]
        code, s = self.call("POST", "/settings/providers", {"providers": clean})
        self.assertEqual(code, 200, s)
        code, s = self.call("POST", "/settings/key", {"key_env": "CUSTOM_API_KEY", "value": key})
        self.assertEqual(code, 200, s)
        return s

    # ---------- test (eseguiti in ordine alfabetico) ----------
    def test_01_auth_and_page(self):
        code, page = self.call("GET", "/", token=None)
        self.assertEqual(code, 200)
        self.assertIn("Account e chiavi", page)
        self.assertEqual(self.call("GET", "/status", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/status", token="sbagliato")[0], 401)
        code, s = self.call("GET", "/status")
        self.assertEqual(code, 200)
        self.assertTrue(any(w["name"] == "local-api" and w["status"] == "online" for w in s["workers"]))

    def test_02_no_account_job_fails_gracefully(self):
        code, s = self.call("GET", "/settings")
        self.assertFalse(s["ready"])
        code, r = self.call("POST", "/jobs/plan", {"adapter": "universal_model", "items": [{"id": "1", "prompt": "ciao"}],
                                                   "params": {"runtime": "remote_api"}})
        self.assertEqual(code, 200, r)
        st = self.wait_job(r["job_id"])
        self.assertEqual(st["status"], "completed")
        code, res = self.call("GET", f"/jobs/{r['job_id']}/results")
        self.assertEqual(res["failed"], 1)
        self.assertIn("Account e chiavi", res["items"][0]["error"])
        self.__class__.nokey_job = r["job_id"]

    def test_03_bad_requests(self):
        self.assertEqual(self.call("GET", "/tasks/next")[0], 400)
        self.assertEqual(self.call("GET", "/tasks/ready?worker_id=x")[0], 400)
        self.assertEqual(self.call("POST", "/jobs/plan", raw=b"{non json")[0], 400)
        self.assertEqual(self.call("POST", "/jobs/plan", raw=b"[1,2]")[0], 400)
        code, r = self.call("POST", "/jobs/plan", {"adapter": "inesistente", "items": ["a"]})
        self.assertEqual(code, 400); self.assertIn("inesistente", r["error"])
        code, r = self.call("POST", "/jobs/plan", {"adapter": "universal_model", "items": []})
        self.assertEqual(code, 400); self.assertIn("step", r["error"])
        self.assertEqual(self.call("POST", "/jobs", {"adapter": "demo", "steps": [{"id": "a", "depends_on": ["zz"]}]})[0], 400)
        self.assertEqual(self.call("GET", "/jobs/nope")[0], 404)
        self.assertEqual(self.call("GET", "/jobs/nope/results")[0], 404)
        self.assertEqual(self.call("POST", "/tasks/fail", {"job_id": "nope", "step_id": "x", "worker_id": "y"})[0], 200)
        self.assertEqual(self.call("POST", "/settings/key", {"key_env": "PATH", "value": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/settings/providers", {"providers": []})[0], 400)
        bad = [{"name": "x", "key_env": "X_KEY", "base_url": "ftp://x", "model": "m"}]
        self.assertEqual(self.call("POST", "/settings/providers", {"providers": bad})[0], 400)
        # il router resta vivo dopo tutti gli errori
        self.assertEqual(self.call("GET", "/health")[0], 200)

    def test_04_connect_account_and_test_button(self):
        s = self.connect_account("good")
        self.assertTrue(s["ready"])
        custom = next(p for p in s["providers"] if p["name"] == "custom")
        self.assertTrue(custom["has_key"])
        self.assertEqual(custom["key_hint"], "impostata")       # chiave corta: non ne mostra le cifre
        self.assertNotIn("good", json.dumps(s), "la chiave non deve mai tornare al browser")
        f = secrets_store.SECRETS_FILE
        self.assertIn("CUSTOM_API_KEY=good", f.read_text())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        code, r = self.call("POST", "/settings/test", {"name": "custom"})
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"], r)
        self.assertIn("OK", r["text"])
        code, r = self.call("POST", "/settings/test", {"name": "groq"})
        self.assertFalse(r["ok"]); self.assertIn("chiave mancante", r["error"])

    def test_05_chat_job(self):
        code, r = self.call("POST", "/jobs/plan", {"adapter": "universal_model",
                                                   "items": [{"id": "1", "prompt": "Ciao pool"}],
                                                   "params": {"runtime": "remote_api", "title": "saluto",
                                                              "options": {"max_tokens": 50}}})
        self.assertEqual(code, 200, r)
        st = self.wait_job(r["job_id"])
        self.assertEqual(st["status"], "completed")
        code, res = self.call("GET", f"/jobs/{r['job_id']}/results")
        self.assertEqual(res["ok"], 1)
        self.assertEqual(res["items"][0]["text"], "OK: Ciao pool")
        self.assertEqual(res["items"][0]["provider"], "custom")
        code, s = self.call("GET", "/status")
        j = next(j for j in s["jobs"] if j["id"] == r["job_id"])
        self.assertEqual((j["items_ok"], j["items_failed"], j["title"]), (1, 0, "saluto"))

    def test_06_batch_job_with_system_prompt(self):
        lines = [f"riga {i}" for i in range(40)]      # 40 righe -> 2 step da 32
        items = [{"id": str(i + 1), "input": l, "prompt": "Classifica:\n\n" + l} for i, l in enumerate(lines)]
        code, r = self.call("POST", "/jobs/plan", {"adapter": "universal_model", "items": items,
                                                   "params": {"runtime": "remote_api",
                                                              "options": {"workers": 4, "system": "sii breve"}}})
        self.assertEqual(code, 200, r)
        st = self.wait_job(r["job_id"], 60)
        self.assertEqual(st["status"], "completed")
        self.assertEqual(len(st["steps"]), 2)
        code, res = self.call("GET", f"/jobs/{r['job_id']}/results")
        self.assertEqual(res["ok"], 40)
        self.assertEqual([it["id"] for it in res["items"]], [str(i + 1) for i in range(40)], "ordine per id")
        self.assertEqual(res["items"][4]["input"], "riga 4")
        sent = [c["body"]["messages"] for c in self.fake.calls if c["body"].get("messages", [{}])[-1].get("content", "").endswith("riga 4")]
        self.assertEqual(sent[0][0], {"role": "system", "content": "sii breve"})

    def test_07_retry_failed_items(self):
        code, r = self.call("POST", f"/jobs/{self.nokey_job}/retry", {})
        self.assertEqual(code, 200, r)
        st = self.wait_job(r["job_id"])
        code, res = self.call("GET", f"/jobs/{r['job_id']}/results")
        self.assertEqual(res["ok"], 1, res)
        # un job tutto riuscito non è rilanciabile
        self.assertEqual(self.call("POST", f"/jobs/{r['job_id']}/retry", {})[0], 400)

    def test_08_failing_step_marks_job_failed_and_dependents(self):
        # payload senza "items": lo step 1 va in errore nel worker
        manifest = {"adapter": "universal_model", "params": {"runtime": "remote_api"},
                    "steps": [{"id": "s1", "depends_on": [], "payload": {"rotto": True}},
                              {"id": "s2", "depends_on": ["s1"], "payload": {"items": [{"id": "2", "prompt": "y"}]}}]}
        code, r = self.call("POST", "/jobs", manifest)
        self.assertEqual(code, 200, r)
        st = self.wait_job(r["job_id"])
        self.assertEqual(st["status"], "failed")
        self.assertEqual(st["steps"], {"s1": "failed", "s2": "failed"})
        code, res = self.call("GET", f"/jobs/{r['job_id']}/results")
        self.assertTrue(any("KeyError" in e for e in res["errors"]), res["errors"])
        self.assertTrue(any("dipendenza fallita" in e for e in res["errors"]), res["errors"])
        code, allst = self.call("GET", "/status")                        # il motivo si vede già nella lista dei lavori
        row = [j for j in allst["jobs"] if j["id"] == r["job_id"]][0]
        self.assertIn("KeyError", row["error"]); self.assertGreaterEqual(row["attempts"], 1)

    def test_084_local_api_worker_gets_long_lease(self):
        """Il worker locale (stesso programma del server) non va dichiarato «caduto» per una pausa breve del telefono."""
        local = [wid for wid, w in self.router.workers.items() if w["profile"].get("api_only") and w["status"] == "online"]
        self.assertTrue(local, "serve il worker locale online")
        gpu = self.router.register({"name": "gpu-lease-test", "gpu": "x", "vram_gb": 14.0})
        with self.router.lock:
            self.router.workers[local[0]]["last_seen"] -= 120
            self.router.workers[gpu]["last_seen"] -= 120
        self.router.reap()
        self.assertEqual(self.router.workers[local[0]]["status"], "online")          # 2 minuti di pausa: ancora in vita
        self.assertEqual(self.router.workers[gpu]["status"], "offline")              # un nodo GPU esterno: si
        with self.router.lock:
            self.router.workers[local[0]]["last_seen"] = time.time()

    def test_085_gpu_jobs_wait_for_gpu_node(self):
        code, r = self.call("POST", "/jobs/plan", {"adapter": "demo", "items": [],
                                                   "params": {"n_steps": 2, "step_seconds": 0.2}})
        self.assertEqual(code, 200, r)
        time.sleep(1.5)
        code, s = self.call("GET", f"/jobs/{r['job_id']}")
        self.assertEqual(set(s["steps"].values()), {"pending"}, "il worker solo-API non deve prendere job GPU")
        code, st = self.call("GET", "/status")
        self.assertGreaterEqual(st["demand"]["nodes_needed"], 1, "il launcher deve vedere che serve una GPU")
        gpu = Worker(self.base, TOKEN, self.store, name="gpu-node", hb=1, poll=0.2, verbose=False)
        threading.Thread(target=gpu.run, kwargs={"max_idle_s": 3}, daemon=True).start()
        self.assertEqual(self.wait_job(r["job_id"])["status"], "completed")
        # un job API con il worker solo-API online non va al nodo GPU
        code, r2 = self.call("POST", "/jobs/plan", {"adapter": "universal_model", "items": ["ciao gpu"],
                                                    "params": {"runtime": "remote_api"}})
        self.wait_job(r2["job_id"])
        code, res = self.call("GET", "/status")
        self.assertTrue(any("step_001 -> local-api" in e["m"] for e in res["events"][-6:]), res["events"][-6:])
        gpu.dead = True

    def test_09_cancel(self):
        self.connect_account("slow")
        self.fake.slow_s = 3
        code, r = self.call("POST", "/jobs/plan", {"adapter": "universal_model",
                                                   "items": [{"id": "1", "prompt": "lento"}],
                                                   "params": {"runtime": "remote_api"}})
        time.sleep(0.5)
        code, c = self.call("POST", f"/jobs/{r['job_id']}/cancel", {})
        self.assertTrue(c["ok"])
        st = self.wait_job(r["job_id"])
        self.assertEqual(st["status"], "failed")
        time.sleep(3)   # il worker finisce: il suo commit deve essere rifiutato, il job resta annullato
        code, s = self.call("GET", f"/jobs/{r['job_id']}")
        self.assertEqual(s["status"], "failed")
        self.connect_account("good")

    def test_10_restart_recovers_jobs_and_results(self):
        code, s = self.call("GET", "/status")
        before = {j["id"]: j["status"] for j in s["jobs"]}
        self.__class__.stop_router()
        self.__class__.start_router()
        code, s = self.call("GET", "/status")
        after = {j["id"]: j["status"] for j in s["jobs"]}
        self.assertEqual(before, after)
        done = [j for j, v in after.items() if v == "completed"]
        code, res = self.call("GET", f"/jobs/{done[-1]}/results")
        self.assertGreater(len(res["items"]), 0)
        # il nuovo worker lavora ancora
        code, r = self.call("POST", "/jobs/plan", {"adapter": "universal_model",
                                                   "items": [{"id": "1", "prompt": "dopo riavvio"}],
                                                   "params": {"runtime": "remote_api"}})
        self.assertEqual(self.wait_job(r["job_id"])["status"], "completed")

    def test_11_remove_key(self):
        code, s = self.call("POST", "/settings/key", {"key_env": "CUSTOM_API_KEY", "value": ""})
        self.assertFalse(s["ready"])
        self.assertNotIn("CUSTOM_API_KEY", os.environ)
        self.assertNotIn("CUSTOM_API_KEY", secrets_store.SECRETS_FILE.read_text())
        code, s = self.call("POST", "/settings/providers/reset", {})
        custom = next(p for p in s["providers"] if p["name"] == "custom")
        self.assertEqual(custom["base_url"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
