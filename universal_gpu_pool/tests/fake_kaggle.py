"""Finto Kaggle per i test: stesso protocollo dell'SDK ufficiale (POST JSON su /v1/<servizio>/<metodo>).

Esegue DAVVERO lo script pubblicato (in un sottoprocesso, con GPOOL_WORKDIR/GPOOL_TMPDIR al posto
di /kaggle/working e /tmp) e poi espone i file prodotti come farebbe Kaggle.
Token validi: "good-token" (utente "tester"). Opzioni per simulare problemi: no_phone, queue_s, page_size.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

USERS = {"good-token": "tester"}


class FakeKaggle:
    def __init__(self, no_phone=False, queue_s=0.3, page_size=4, timeout_kill=None, port=0, unzip=True, dataset_delay=0.3, deny_datasets=False, deny_status=False, strict_ready=False, silent_fail_first=0):
        self.silent_fail_first, self.saves = silent_fail_first, 0
        self.deny_datasets = deny_datasets
        self.deny_status, self.strict_ready = deny_status, strict_ready
        self.no_phone, self.queue_s, self.page_size = no_phone, queue_s, page_size
        self.unzip, self.dataset_delay = unzip, dataset_delay        # unzip: Kaggle estrae gli ZIP dei dataset
        self.blobs, self.datasets, self.puts = {}, {}, []
        self.kernels = {}        # (user, slug) -> dict
        self.calls = []          # (service/method, auth)
        self.root = Path(tempfile.mkdtemp(prefix="fake_kaggle_"))
        self._lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, raw=None, ctype="application/json"):
                data = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/files/"):
                    rel = unquote(self.path[len("/files/"):])
                    slug, _, name = rel.partition("/")
                    k = next((k for k in fake.kernels if k[1] == slug), None)
                    p = fake.kernels[k]["dir"] / "working" / name if k else None
                    if p and p.is_file():
                        return self._send(200, None, raw=p.read_bytes(), ctype="application/octet-stream")
                return self._send(404, {"code": 404, "message": "not found"})

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0)
                token = self.path.rsplit("/", 1)[-1]
                if token not in fake.blobs:
                    return self._send(404, {"code": 404, "message": "unknown upload"})
                data = b""
                while len(data) < n:
                    chunk = self.rfile.read(min(1 << 16, n - len(data)))
                    if not chunk:
                        break
                    data += chunk
                if len(data) != n or n != fake.blobs[token]["size"]:
                    return self._send(400, {"code": 400, "message": "size mismatch"})
                fake.blobs[token]["data"] = data
                fake.puts.append((token, n))
                return self._send(200, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                path = self.path
                auth = self.headers.get("Authorization", "")
                with fake._lock:
                    fake.calls.append((path, auth))
                if not path.startswith("/v1/"):
                    return self._send(404, {"code": 404, "message": "no"})
                svc, _, method = path[4:].partition("/")
                token = auth[7:] if auth.startswith("Bearer ") else None
                if token not in USERS and not (svc.startswith("security") and method == "IntrospectToken"):
                    return self._send(401, {"code": 401, "message": "Unauthorized"})
                try:
                    return self._dispatch(svc, method, body, token)
                except Exception as e:     # un bug del finto non deve sembrare un errore di Kaggle
                    return self._send(500, {"code": 500, "message": f"fake error {type(e).__name__}: {e}"})

            def _dispatch(self, svc, method, body, token):
                if (svc, method) == ("security.OAuthService", "IntrospectToken"):
                    u = USERS.get(body.get("token"))
                    return self._send(200, {"active": bool(u), "username": u or ""})
                if fake.deny_datasets and svc in ("blobs.BlobApiService", "datasets.DatasetApiService"):
                    return self._send(403, {"code": 403, "message": "Permission 'datasets.create' denied"})
                if (svc, method) == ("blobs.BlobApiService", "StartBlobUpload"):
                    assert body.get("type") == "DATASET" and body.get("name") and body.get("contentLength") > 0
                    tok = "tok" + str(len(fake.blobs) + 1) + os.urandom(3).hex()
                    fake.blobs[tok] = {"name": body["name"], "size": body["contentLength"], "data": None}
                    return self._send(200, {"token": tok, "createUrl": f"http://127.0.0.1:{fake.port}/upload/{tok}"})
                if svc == "datasets.DatasetApiService":
                    key = (body.get("ownerSlug"), body.get("slug") or body.get("datasetSlug"))
                    if method == "CreateDataset":
                        assert body.get("isPrivate") is True and body.get("licenseName") and body.get("title")
                        assert key[0] == USERS[token] and key[1] == "-".join(body["title"].lower().split())
                        files = []
                        for f in body.get("files", []):
                            b = fake.blobs.get(f.get("token"))
                            if not b or b["data"] is None:
                                return self._send(400, {"code": 400, "message": "file token without uploaded data"})
                            files.append((b["name"], b["data"]))
                        fake.datasets[key] = {"files": files, "t0": time.time()}
                        return self._send(200, {"ref": f"{key[0]}/{key[1]}", "status": "ok", "url": "https://x"})
                    if method == "GetDatasetStatus":
                        if fake.deny_status:
                            return self._send(403, {"code": 403, "message": "Permission 'datasets.get' denied"})
                        d = fake.datasets.get(key)
                        if not d:
                            return self._send(404, {"code": 404, "message": "dataset not found"})
                        return self._send(200, {"status": "READY" if time.time() - d["t0"] >= fake.dataset_delay else "BLOBS_RECEIVED"})
                    if method == "DeleteDataset":
                        fake.datasets.pop(key, None)
                        return self._send(200, {})
                    return self._send(404, {"code": 404, "message": "unknown method"})
                if svc != "kernels.KernelsApiService":
                    return self._send(404, {"code": 404, "message": "unknown service"})
                if method == "GetAcceleratorQuotaStatistics":
                    return self._send(200, {"quotaRefreshTime": "2026-12-01T00:00:00Z",
                                            "gpuQuota": {"timeUsed": "7200s", "timeReserved": "0s",
                                                         "totalTimeAllowed": "108000s", "minimumTimeAllowed": "108000s"}})
                if method == "SaveKernel":
                    for k in ("slug", "newTitle", "text"):
                        if not body.get(k):
                            return self._send(400, {"code": 400, "message": f"missing {k}"})
                    assert body.get("kernelType") == "script" and body.get("language") == "python"
                    assert body.get("isPrivate") is True
                    if fake.no_phone and (body.get("enableGpu") or body.get("enableInternet")):
                        return self._send(403, {"code": 403, "message": "Phone verification required"})
                    user, _, slug = body["slug"].partition("/")
                    assert user == USERS[token], f"slug utente sbagliato: {body['slug']}"
                    assert slug == "-".join(body["newTitle"].lower().split()), "lo slug deve derivare dal titolo"
                    if fake.strict_ready:                  # come Kaggle vero: dataset non ancora pronto -> sorgente non valida
                        for ref in body.get("datasetDataSources") or []:
                            ds0 = fake.datasets.get(tuple(ref.split("/", 1)))
                            if not ds0 or time.time() - ds0["t0"] < fake.dataset_delay:
                                fake.invalid_tries = getattr(fake, "invalid_tries", 0) + 1
                                return self._send(200, {"invalidDatasetSources": [ref]})
                    d = fake.root / slug
                    shutil.rmtree(d, ignore_errors=True)
                    (d / "working").mkdir(parents=True)
                    (d / "tmp").mkdir()
                    (d / "script.py").write_text(body["text"], encoding="utf-8")
                    k = {"dir": d, "t0": time.time(), "body": body, "proc": None, "rc": None}
                    fake.kernels[(user, slug)] = k
                    fake.saves += 1
                    if fake.silent_fail_first > 0:         # il notebook non parte: ERROR subito, nessun registro, nessun file
                        fake.silent_fail_first -= 1
                        k["rc"] = 1
                        (d / "kernel.log").write_text("")
                        return self._send(200, {"ref": f"/code/{user}/{slug}", "url": f"https://x/{user}/{slug}", "versionNumber": 1})
                    env = dict(os.environ, GPOOL_WORKDIR=str(d / "working"), GPOOL_TMPDIR=str(d / "tmp"))
                    k["datasets"] = body.get("datasetDataSources") or []
                    if k["datasets"]:                       # monta i dataset come /kaggle/input/<slug>/...
                        import zipfile, io
                        inp = d / "input"
                        for ref in k["datasets"]:
                            ds = fake.datasets.get(tuple(ref.split("/", 1)))
                            assert ds, f"dataset sconosciuto: {ref}"
                            root = inp / ref.split("/", 1)[1]
                            root.mkdir(parents=True, exist_ok=True)
                            for name, data in ds["files"]:
                                if fake.unzip and name.lower().endswith(".zip"):
                                    zipfile.ZipFile(io.BytesIO(data)).extractall(root / Path(name).stem)
                                else:
                                    (root / name).write_bytes(data)
                        env["GPOOL_INPUT_DIR"] = str(inp)
                    log = open(d / "kernel.log", "w")

                    def go():
                        time.sleep(fake.queue_s)
                        k["proc"] = subprocess.Popen([sys.executable, str(d / "script.py")], env=env, stdout=log,
                                                     stderr=subprocess.STDOUT, cwd=str(d))
                        k["rc"] = k["proc"].wait()
                        log.close()
                    threading.Thread(target=go, daemon=True).start()
                    return self._send(200, {"ref": f"/code/{user}/{slug}", "url": f"https://x/{user}/{slug}", "versionNumber": 1})
                key = (body.get("userName"), body.get("kernelSlug"))
                if method in ("GetKernelSessionStatus", "ListKernelSessionOutput", "DeleteKernel") and key not in fake.kernels:
                    return self._send(404, {"code": 404, "message": "kernel not found"})
                if method == "GetKernelSessionStatus":
                    k = fake.kernels[key]
                    if k["rc"] is not None:
                        return self._send(200, {"status": "COMPLETE" if k["rc"] == 0 else "ERROR",
                                                "failureMessage": "" if k["rc"] == 0 else f"script exit {k['rc']}"})
                    return self._send(200, {"status": "RUNNING" if k["proc"] else "QUEUED"})
                if method == "ListKernelSessionOutput":
                    k = fake.kernels[key]
                    allf = sorted(p for p in (k["dir"] / "working").rglob("*") if p.is_file())
                    start = int(body.get("pageToken") or 0)
                    page = allf[start:start + fake.page_size]
                    nxt = str(start + fake.page_size) if start + fake.page_size < len(allf) else None
                    out = {"files": [{"url": f"http://127.0.0.1:{fake.port}/files/{key[1]}/{p.relative_to(k['dir'] / 'working').as_posix()}",
                                      "fileName": p.relative_to(k["dir"] / "working").as_posix()} for p in page],
                           "log": (k["dir"] / "kernel.log").read_text(errors="replace")}
                    if nxt:
                        out["nextPageToken"] = nxt
                    return self._send(200, out)
                if method == "DeleteKernel":
                    fake.kernels.pop(key, None)
                    return self._send(200, {})
                return self._send(404, {"code": 404, "message": "unknown method " + method})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown(); self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)


if __name__ == "__main__":      # python -m tests.fake_kaggle <porta>
    k = FakeKaggle(port=int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    print(f"fake Kaggle su {k.base}", flush=True)
    k.server.serve_forever()
