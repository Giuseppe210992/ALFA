import base64, io, json, os, subprocess, tempfile, threading, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_bundle(root=ROOT):
    """Zip del solo codice (core, worker, adapters, pipelines), incorporato nello script del kernel."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in ("core", "worker", "adapters", "pipelines"):
            for p in sorted((root / d).rglob("*.py")):
                z.write(p, f"universal_gpu_pool/{p.relative_to(root)}")
    return buf.getvalue()


KERNEL_TEMPLATE = '''import base64, io, json, os, subprocess, sys, zipfile
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "boto3"], check=False)
zipfile.ZipFile(io.BytesIO(base64.b64decode("{zip_b64}"))).extractall("/kaggle/working")
os.chdir("/kaggle/working/universal_gpu_pool"); sys.path.insert(0, os.getcwd())
CFG = json.loads({cfg_json!r})
try:  # i segreti stanno nei Kaggle Secrets del notebook, MAI nel codice
    from kaggle_secrets import UserSecretsClient
    us = UserSecretsClient()
    for k in (
        "POOL_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "SCALEWAY_API_KEY",
        "GROQ_API_KEY", "GEMINI_API_KEY", "CUSTOM_API_KEY",
    ):
        try: os.environ[k] = us.get_secret(k)
        except Exception: pass
except ImportError:
    pass
from core.state_store import open_store
from worker.worker_daemon import Worker
Worker(CFG["router_url"], os.environ["POOL_TOKEN"], open_store(CFG), CFG["worker_name"],
       hb=CFG["heartbeat_interval"], session_limit_s=CFG["session_limit_s"]).run(max_idle_s=CFG["max_idle_s"])
'''


def render_kernel_script(cfg, slot, bundle):
    wcfg = {k: cfg.get(k) for k in ("store_backend", "store_root", "s3_bucket", "s3_prefix", "s3_endpoint_url", "s3_region",
                                    "heartbeat_interval", "session_limit_s")}
    wcfg.update(router_url=cfg["router_public_url"], worker_name=slot, max_idle_s=cfg.get("worker_max_idle_s", 600))
    return KERNEL_TEMPLATE.format(zip_b64=base64.b64encode(bundle).decode(), cfg_json=json.dumps(wcfg))


class KaggleBackend:
    """Usa la CLI ufficiale `kaggle` (pip install kaggle; KAGGLE_USERNAME / KAGGLE_KEY nell'ambiente)."""

    def __init__(self, cfg, workdir=os.path.join(tempfile.gettempdir(), "kaggle_push")):
        self.cfg, self.workdir = cfg, Path(workdir)

    def _run(self, args):
        return subprocess.run(["kaggle"] + args, capture_output=True, text=True, timeout=180)

    def check(self):
        """Errori di configurazione che renderebbero inutile lanciare un nodo."""
        problems = []
        if self.cfg.get("store_backend", "local") != "s3":
            problems.append("i worker Kaggle richiedono uno storage condiviso: imposta store_backend: s3 "
                            "(R2/B2/S3/MinIO) in config/pool_config.yaml — con 'local' i risultati "
                            "resterebbero sul nodo Kaggle e il router non li vedrebbe mai")
        url = str(self.cfg.get("router_public_url") or "")
        if not url.startswith("https://") or "example.com" in url:
            problems.append("router_public_url deve essere l'indirizzo HTTPS pubblico del router "
                            "(es. un tunnel Cloudflare o il tuo dominio con Caddy)")
        slots = self.cfg.get("launcher_slots") or []
        if not slots or any("TUO_USERNAME" in s for s in slots):
            problems.append("launcher_slots: sostituisci TUO_USERNAME_KAGGLE con il tuo username Kaggle")
        return problems

    def prepare(self, slot):
        d = self.workdir / slot.replace("/", "__")
        d.mkdir(parents=True, exist_ok=True)
        (d / "worker_kernel.py").write_text(render_kernel_script(self.cfg, slot, build_bundle()))
        # Boolean JSON nativi (non stringhe "true"): la CLI Kaggle attuale li accetta meglio.
        (d / "kernel-metadata.json").write_text(json.dumps({
            "id": slot,
            "title": slot.split("/")[-1],
            "code_file": "worker_kernel.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": True,
            "enable_internet": True,
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
        }, indent=1))
        return d

    def push(self, slot):
        r = self._run(["kernels", "push", "-p", str(self.prepare(slot))])
        if r.returncode:
            raise RuntimeError((r.stdout + r.stderr).strip()[:300])

    def status(self, slot):
        r = self._run(["kernels", "status", slot])
        out = (r.stdout + r.stderr).lower()
        if r.returncode:
            return "unknown"
        for key, val in (("running", "running"), ("queued", "queued"), ("complete", "complete"),
                         ("error", "error"), ("cancel", "cancelled")):
            if key in out:
                return val
        return "unknown"


class SimBackend:
    """Per i test: ogni 'lancio' è un Worker in un thread."""

    def __init__(self, make_worker, max_idle_s=3):
        self.make_worker, self.max_idle_s, self.threads = make_worker, max_idle_s, {}

    def push(self, slot):
        w = self.make_worker(slot)
        t = threading.Thread(target=w.run, kwargs={"max_idle_s": self.max_idle_s}, daemon=True)
        self.threads[slot] = t
        t.start()

    def status(self, slot):
        t = self.threads.get(slot)
        return "running" if t and t.is_alive() else "complete"


# ---------------------------------------------------------------------------
# On-demand (RunPod) – percorso a pagamento, acceso solo con coda heavy
# ---------------------------------------------------------------------------
class OnDemandLauncherBackend:
    """Wrapper che adatta OnDemandBackend all'interfaccia push/status del launcher.

    Usa un singolo "slot" logico 'on_demand'. Il vero controllo (accensione /
    spegnimento, tetti di spesa) è demand-driven via scale().
    """

    def __init__(self, on_demand_backend):
        from core.on_demand_backend import OnDemandBackend
        self.be = on_demand_backend if isinstance(on_demand_backend, OnDemandBackend) else OnDemandBackend()
        self._last_status = "complete"

    def push(self, slot):
        # Il launcher chiama push quando serve un nodo. Deleghiamo a scale.
        res = self.be.scale({"nodes_needed": 1})
        self._last_status = "running" if res.get("active") else "complete"
        return res

    def status(self, slot):
        # Aggiorna contabilita' e stato
        res = self.be.scale({"nodes_needed": 0 if self._last_status == "complete" else 1})
        active = bool(res.get("active"))
        self._last_status = "running" if active else "complete"
        return self._last_status

    def scale(self, demand):
        return self.be.scale(demand)

    def shutdown(self):
        self.be.shutdown()
