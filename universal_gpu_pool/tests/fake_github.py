"""Finto GitHub per i test: API (/repos/..), file raw e repository git veri clonabili con `git clone`."""
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

DEMO_APP = '''import argparse, os, sys
ap = argparse.ArgumentParser()
ap.add_argument("--text", required=True)
ap.add_argument("--repeat", type=int, default=1)
ap.add_argument("--input")
ap.add_argument("--out", default="results")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
lines = [a.text] * a.repeat
if a.input:
    n = len(open(a.input, encoding="utf-8").read().splitlines())
    lines.append("righe nel file di input: %d" % n)
open(os.path.join(a.out, "result.txt"), "w", encoding="utf-8").write("\\n".join(lines) + "\\n")
print("fatto", len(lines))
if os.environ.get("DEMO_FAIL"):
    sys.exit(3)
'''
DEMO_README = '''# Demo App

Ripete un testo quante volte vuoi e salva il risultato in un file.

```bash
pip install -r requirements.txt
python app.py --text "ciao" --repeat 3 --out results
```
'''


def make_repo(base, owner, name, files=None):
    """Crea <base>/<owner>/<name>.git come repository git con un commit."""
    d = Path(base) / owner / f"{name}.git"
    d.mkdir(parents=True, exist_ok=True)
    files = files or {"README.md": DEMO_README, "app.py": DEMO_APP, "requirements.txt": "# nessuna dipendenza\n"}
    for rel, txt in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "."], ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=d, check=True, env=env, capture_output=True)
    return d


class FakeGitHub:
    def __init__(self, base):
        self.base = Path(base)
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, raw=None):
                data = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json" if raw is None else "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                parts = [unquote(p) for p in u.path.split("/") if p]
                fake.requests.append(u.path)
                if parts[:1] == ["raw"] and len(parts) >= 5:
                    f = fake.base / parts[1] / f"{parts[2]}.git" / "/".join(parts[4:])
                    return self._send(200, raw=f.read_bytes()) if f.is_file() else self._send(404, {"message": "Not Found"})
                if parts[:1] == ["repos"] and len(parts) >= 3:
                    d = fake.base / parts[1] / f"{parts[2]}.git"
                    if not d.is_dir():
                        return self._send(404, {"message": "Not Found"})
                    if len(parts) == 3:
                        return self._send(200, {"default_branch": "main", "description": "Programma di prova",
                                                "stargazers_count": 5, "license": {"spdx_id": "MIT"}, "size": 10,
                                                "language": "Python", "archived": False})
                    if parts[3:5] == ["git", "trees"]:
                        tree = [{"path": p.relative_to(d).as_posix(), "type": "blob", "size": p.stat().st_size}
                                for p in sorted(d.rglob("*")) if p.is_file() and ".git" not in p.relative_to(d).parts]
                        return self._send(200, {"tree": tree, "truncated": False})
                return self._send(404, {"message": "Not Found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.api = f"http://127.0.0.1:{self.port}"

    def env(self):
        return {"GPOOL_GITHUB_API": self.api, "GPOOL_GITHUB_RAW": self.api + "/raw",
                "GPOOL_GITHUB_CLONE_BASE": "file://" + str(self.base)}

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown(); self.server.server_close()


if __name__ == "__main__":      # python -m tests.fake_github <porta> <cartella>  (crea o/demo)
    import sys
    base = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="fake_gh_"))
    make_repo(base, "o", "demo")
    g = FakeGitHub(base)
    if len(sys.argv) > 1 and int(sys.argv[1]):
        g.server.server_close()
        g.server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), g.server.RequestHandlerClass)
        g.port = int(sys.argv[1]); g.api = f"http://127.0.0.1:{g.port}"
    print("fake GitHub " + json.dumps(g.env()), flush=True)
    g.server.serve_forever()
