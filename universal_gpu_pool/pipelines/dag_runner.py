"""Client + flusso Preprocess -> GPU Pool -> Merge."""
import json, time, urllib.error, urllib.request
from urllib.parse import urlparse

# Verso il router locale non si passa mai da un proxy di sistema (Windows/macOS lo leggono
# dalle impostazioni e romperebbero le chiamate a 127.0.0.1).
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PoolHTTPError(RuntimeError):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:300]}")
        self.code, self.body = code, body


class PoolClient:
    def __init__(self, url, token, timeout=15):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        host = (urlparse(self.url).hostname or "").lower()
        self._open = _DIRECT.open if host in ("127.0.0.1", "localhost", "::1") else urllib.request.urlopen

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Pool-Token": self.token, "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise PoolHTTPError(e.code, e.read().decode(errors="replace")) from None


def run_job(client, adapter, items, params, store=None, dest=None, poll=2.0, preprocess=None, timeout=None):
    if preprocess:
        items = preprocess(items)
    jid = client.call("POST", "/jobs/plan", {"adapter": adapter, "items": items, "params": params})["job_id"]
    t0 = time.time()
    while True:
        s = client.call("GET", f"/jobs/{jid}")
        if s["status"] == "completed":
            break
        if s["status"] == "failed" or "failed" in s["steps"].values():
            raise RuntimeError(f"job {jid} fallito: {s}")
        if timeout and time.time() - t0 > timeout:
            raise TimeoutError(f"job {jid} non completato entro {timeout}s: {s}")
        time.sleep(poll)
    if store and dest:
        from adapters import get_adapter
        manifest = store.get_json(f"jobs/{jid}/manifest.json")
        get_adapter(adapter)().merge(store, jid, manifest, dest)
    return jid
