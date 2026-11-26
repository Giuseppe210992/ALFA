"""Client + flusso Preprocess -> GPU Pool -> Merge."""
import json, time, urllib.request
from pathlib import Path


class PoolClient:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Pool-Token": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())


def run_job(client, adapter, items, params, store=None, dest=None, poll=2.0, preprocess=None):
    if preprocess:
        items = preprocess(items)
    jid = client.call("POST", "/jobs/plan", {"adapter": adapter, "items": items, "params": params})["job_id"]
    while True:
        s = client.call("GET", f"/jobs/{jid}")
        if s["status"] == "completed":
            break
        if "failed" in s["steps"].values():
            raise RuntimeError(f"job {jid} fallito: {s}")
        time.sleep(poll)
    if store and dest:
        from adapters import get_adapter
        manifest = store.get_json(f"jobs/{jid}/manifest.json")
        get_adapter(adapter)().merge(store, jid, manifest, dest)
    return jid
