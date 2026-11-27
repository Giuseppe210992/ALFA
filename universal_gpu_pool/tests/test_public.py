"""Modo «server pubblico» (es. Hugging Face Spaces): token solo dal segreto, pulizia dei file con chiavi, limite tentativi."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.aggregator_api import LoginThrottle  # noqa: E402

TOKEN = "T" * 40
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path, token=TOKEN, xff=None):
    h = {"X-Pool-Token": token}
    if xff:
        h["X-Forwarded-For"] = xff
    try:
        with OPENER.open(urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h), timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class PublicModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pub_"))
        self.app = self.tmp / "app"
        shutil.copytree(ROOT, self.app, ignore=shutil.ignore_patterns("__pycache__", "e2e_*", "gpu_pool_store", ".git", "*.pyc"))
        (self.app / "config" / "secrets.env").write_text("KAGGLE_API_TOKEN=CHIAVE-VAZATA\nGROQ_API_KEY=gsk_vazata\n")
        (self.app / "config" / "local_config.yaml").write_text('token: "' + "x" * 30 + '"\n')
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill(); p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, env_extra, port=None):
        port = port or free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GPOOL_", "KAGGLE_", "GROQ_", "SPACE_"))}
        env.update({"GPOOL_PUBLIC": "1", "PYTHONUNBUFFERED": "1", **env_extra})
        p = subprocess.Popen([sys.executable, "run_webapp.py", "--host", "0.0.0.0", "--port", str(port), "--no-browser"],
                             cwd=self.app, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        return p, port

    def wait_up(self, p, port):
        t0 = time.time()
        while time.time() - t0 < 20:
            if p.poll() is not None:
                self.fail("il server è uscito: " + p.stdout.read())
            try:
                with OPENER.open(f"http://127.0.0.1:{port}/", timeout=2):
                    return
            except OSError:
                time.sleep(0.2)
        self.fail("il server non è partito")

    def test_refuses_to_start_without_strong_secret(self):
        for env in ({}, {"GPOOL_TOKEN": "corto"}, {"GPOOL_TOKEN": "change-me"}):
            p, port = self.start(env)
            out, _ = p.communicate(timeout=20)
            self.assertEqual(p.returncode, 2, (env, out))
            self.assertIn("GPOOL_TOKEN", out); self.assertIn("24 caratteri", out)
        # i file con segreti arrivati nel pacchetto sono stati cancellati comunque
        self.assertFalse((self.app / "config" / "secrets.env").exists())
        self.assertFalse((self.app / "config" / "local_config.yaml").exists())

    def test_token_only_from_secret_and_leaked_files_ignored(self):
        p, port = self.start({"GPOOL_TOKEN": TOKEN, "KAGGLE_API_TOKEN": "dal-segreto"})
        self.wait_up(p, port)
        self.assertEqual(get(port, "/health")[0], 200)
        self.assertEqual(get(port, "/health", token="x" * 30)[0], 401, "il token del file portato nel pacchetto non deve valere")
        self.assertFalse((self.app / "config" / "secrets.env").exists(), "chiavi vazate cancellate")
        code, g = get(port, "/gpu")
        self.assertTrue(g["kaggle"]["connected"], "le chiavi dai segreti del Space si leggono dall'ambiente")
        code, s = get(port, "/settings")
        self.assertFalse(any(pp["has_key"] for pp in s["providers"]), "la chiave GROQ vazata non deve essere stata caricata")
        p.terminate()
        out = p.stdout.read()
        self.assertNotIn(TOKEN, out, "il token non va mai stampato nei log del server")
        self.assertIn("Cancellato config/secrets.env", out)

    def test_login_throttle_per_client(self):
        p, port = self.start({"GPOOL_TOKEN": TOKEN})
        self.wait_up(p, port)
        for i in range(8):
            self.assertEqual(get(port, "/status", token="sbagliato", xff="203.0.113.7")[0], 401)
        code, r = get(port, "/status", token=TOKEN, xff="203.0.113.7")
        self.assertEqual(code, 429); self.assertIn("Troppi tentativi", r["error"])
        self.assertEqual(get(port, "/status", token=TOKEN, xff="198.51.100.9")[0], 200, "un altro utente non è bloccato")
        self.assertEqual(get(port, "/status", token=TOKEN, xff="203.0.113.7, 10.0.0.1")[0], 429, "conta il primo IP della catena")

    def test_success_resets_failures(self):
        p, port = self.start({"GPOOL_TOKEN": TOKEN})
        self.wait_up(p, port)
        for _ in range(3):
            for _ in range(5):
                get(port, "/status", token="sbagliato", xff="192.0.2.1")
            self.assertEqual(get(port, "/status", token=TOKEN, xff="192.0.2.1")[0], 200)


class ThrottleUnitTest(unittest.TestCase):
    def test_lock_and_expiry(self):
        t = LoginThrottle(max_failures=3, window_s=60, lock_s=1)
        self.assertEqual(t.blocked("a"), 0)
        for _ in range(3):
            t.fail("a")
        self.assertGreater(t.blocked("a"), 0)
        self.assertEqual(t.blocked("b"), 0)
        time.sleep(1.2)
        self.assertEqual(t.blocked("a"), 0, "il blocco scade")

    def test_window_forgets_old_failures(self):
        t = LoginThrottle(max_failures=3, window_s=1, lock_s=60)
        t.fail("a"); t.fail("a")
        time.sleep(1.2)
        t.fail("a")
        self.assertEqual(t.blocked("a"), 0)

    def test_memory_bounded(self):
        t = LoginThrottle(max_failures=100)
        for i in range(6000):
            t.fail(f"ip{i}")
        self.assertLess(len(t.fails), 5001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
