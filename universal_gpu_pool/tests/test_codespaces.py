"""GitHub Codespaces: .devcontainer, script di avvio, attivazione automatica, chiavi conservate."""
import json
import os
import shutil
import signal
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
REPO = ROOT.parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TOKEN = "C" * 40


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(port, path, body=None, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Pool-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with OPENER.open(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class DevcontainerTest(unittest.TestCase):
    def setUp(self):
        p = REPO / ".devcontainer" / "devcontainer.json"
        if not p.exists():
            self.skipTest("eseguito fuori dal repository completo")
        self.cfg = json.loads(p.read_text())

    def test_config_is_what_codespaces_needs(self):
        c = self.cfg
        self.assertIn("python", c["image"])
        self.assertEqual(c["forwardPorts"], [8765])
        self.assertEqual(c["portsAttributes"]["8765"]["onAutoForward"], "openBrowser")
        self.assertIn("avvia-codespaces.sh", c["postStartCommand"])
        self.assertTrue((REPO / c["postStartCommand"].split()[-1]).is_file())
        for f in c["customizations"]["codespaces"]["openFiles"]:
            self.assertTrue((REPO / f).is_file(), f)

    def test_script_is_valid_bash_and_executable(self):
        s = ROOT / "avvia-codespaces.sh"
        self.assertTrue(os.access(s, os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", str(s)]).returncode, 0)
        self.assertNotIn("--public", s.read_text(), "la porta resta privata: nessuna opzione per renderla pubblica")


class CodespacesRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cs_"))
        self.app = self.tmp / "universal_gpu_pool"
        shutil.copytree(ROOT, self.app, ignore=shutil.ignore_patterns("__pycache__", "e2e_*", "gpu_pool_store", "*.pyc"))
        for f in ("secrets.env", "local_config.yaml", "providers.json"):
            if (self.app / "config" / f).exists():
                (self.app / "config" / f).unlink()
        (self.app / "config" / "secrets.env").write_text("GROQ_API_KEY=gsk_dell_utente\n")
        self.port = free_port()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GPOOL_", "SPACE_", "KAGGLE_", "GROQ_"))}
        self.env.update({"CODESPACE_NAME": "testcs", "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev",
                         "GPOOL_PORT": str(self.port)})

    def tearDown(self):
        self.stop_server()
        time.sleep(0.3)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stop_server(self):
        subprocess.run(["pkill", "-f", f"run_webapp.py --host 0.0.0.0 --port {self.port}"], capture_output=True)

    def run_script(self):
        return subprocess.run(["bash", str(self.app / "avvia-codespaces.sh")], env=self.env, capture_output=True, text=True, timeout=60)

    def test_starts_itself_activates_and_keeps_user_files(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"https://testcs-{self.port}.app.github.dev/", r.stdout)
        self.assertEqual(call(self.port, "/claim/status")[1], {"claimed": False, "public": True})
        self.assertEqual(call(self.port, "/claim", {"secret": TOKEN})[0], 200)
        code, s = call(self.port, "/settings", token=TOKEN)
        self.assertTrue(next(p for p in s["providers"] if p["name"] == "groq")["has_key"],
                        "su Codespaces le chiavi salvate nel disco vanno conservate e usate")
        self.assertTrue((self.app / "config" / "secrets.env").exists())
        self.assertIn("già in esecuzione", self.run_script().stdout, "una seconda esecuzione non avvia un secondo server")

    def test_restart_of_codespace_reclaims_with_same_device_code(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(call(self.port, "/claim", {"secret": TOKEN})[0], 200)
        self.stop_server(); time.sleep(1)                                          # il codespace si spegne e si riaccende
        r = self.run_script()
        self.assertIn(f"-{self.port}.app.github.dev/", r.stdout)
        self.assertEqual(call(self.port, "/claim/status")[1]["claimed"], False)
        self.assertEqual(call(self.port, "/claim", {"secret": TOKEN})[0], 200)
        self.assertEqual(call(self.port, "/status", token=TOKEN)[0], 200)
        self.assertTrue((self.app / "config" / "secrets.env").exists(), "le chiavi sopravvivono al riavvio")
        log = Path("/tmp/gpool.log").read_text(errors="replace")
        self.assertNotIn(TOKEN, log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
