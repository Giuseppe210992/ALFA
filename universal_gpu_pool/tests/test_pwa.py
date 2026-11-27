"""App installabile: manifest, service worker e icone sono pubblici (anche a istanza non attivata) e validi."""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(port, path):
    try:
        with OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "non è un PNG"
    return struct.unpack(">II", data[16:24])


class PwaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pwa_")
        cls.app = os.path.join(cls.tmp, "app")          # copia: in modalità pubblica il server cancella file di config
        shutil.copytree(ROOT, cls.app, ignore=shutil.ignore_patterns("__pycache__", "e2e_*", "gpu_pool_store", ".git", "*.pyc"))
        cls.port = free_port()
        env = dict(os.environ, GPOOL_PUBLIC="1", GPOOL_PORT=str(cls.port))
        env.pop("GPOOL_TOKEN", None)                      # istanza NON attivata: i file pubblici devono rispondere comunque
        cls.proc = subprocess.Popen([sys.executable, os.path.join(cls.app, "run_webapp.py"), "--no-browser", "--port", str(cls.port)],
                                    cwd=cls.app, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                if fetch(cls.port, "/claim/status")[0] == 200:
                    break
            except Exception:
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_manifest(self):
        code, h, body = fetch(self.port, "/manifest.webmanifest")
        self.assertEqual(code, 200)
        self.assertIn("manifest+json", h.get("Content-Type", ""))
        m = json.loads(body)
        self.assertEqual(m["display"], "standalone")
        self.assertEqual(m["start_url"], "/")
        purposes = {i["purpose"] for i in m["icons"]}
        self.assertIn("maskable", purposes)
        for i in m["icons"]:                              # ogni icona dichiarata esiste ed ha la dimensione dichiarata
            c, _, data = fetch(self.port, i["src"])
            self.assertEqual(c, 200, i["src"])
            w = int(i["sizes"].split("x")[0])
            self.assertEqual(png_size(data), (w, w), i["src"])

    def test_service_worker(self):
        code, h, body = fetch(self.port, "/sw.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", h.get("Content-Type", ""))
        self.assertIn(b"navigate", body)
        self.assertNotIn(b"/jobs", body)                  # non tocca mai le chiamate API

    def test_favicon_and_page_links(self):
        self.assertEqual(fetch(self.port, "/favicon.ico")[0], 200)
        html = fetch(self.port, "/")[2].decode("utf-8")
        for needle in ('rel="manifest"', 'rel="apple-touch-icon"', "theme-color", "beforeinstallprompt"):
            self.assertIn(needle, html)

    def test_api_still_protected(self):
        self.assertIn(fetch(self.port, "/jobs")[0], (401, 403))


if __name__ == "__main__":
    unittest.main()
