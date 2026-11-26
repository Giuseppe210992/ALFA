"""Storage condiviso con due backend: LocalStore (cartella, es. Drive montato) e S3Store
(S3/R2/B2/MinIO). Stessa API per router e worker.

Promozione di un output in 2 fasi (immune ai "worker zombie"):
  1. il worker fa stage_output(): carica in outputs/<step>/<worker_id>/ + digest
  2. il ROUTER, unico scrittore dello stato, fa finalize_output(): verifica e scrive il puntatore
     outputs/<step>/_COMMITTED.json. Chi legge (fetch_output) segue solo quel puntatore."""
import hashlib, json, os, shutil, tempfile
from pathlib import Path


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dir_digest(d):
    h = hashlib.sha256()
    for p in sorted(Path(d).rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(d)).encode())
            h.update(sha256_file(p).encode())
    return h.hexdigest()


class _Base:
    dir_digest = staticmethod(dir_digest)
    scratch = Path(tempfile.gettempdir()) / "gpu_pool_scratch"

    def tmp_dir(self, job_id, step_id, worker_id):
        d = self.scratch / job_id / f"{step_id}_{worker_id}"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        return d


class LocalStore(_Base):
    def __init__(self, root):
        self.root = Path(root)
        for d in ("models_cache", "jobs"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def put_json(self, key, obj):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def get_json(self, key, default=None):
        try:
            with open(self.root / key) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def list_job_ids(self):
        return [p.parent.name for p in (self.root / "jobs").glob("*/manifest.json")]

    def tmp_dir(self, job_id, step_id, worker_id):   # stesso filesystem => rename atomico
        d = self.root / "jobs" / job_id / "outputs" / f".tmp_{step_id}_{worker_id}"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        return d

    def _out(self, job_id, step_id):
        return self.root / "jobs" / job_id / "outputs" / step_id

    def stage_output(self, tmp, job_id, step_id, worker_id):
        digest = dir_digest(tmp)
        dest = self._out(job_id, step_id) / worker_id
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        return digest

    def finalize_output(self, job_id, step_id, worker_id, digest):
        dest = self._out(job_id, step_id) / worker_id
        if not dest.exists() or dir_digest(dest) != digest:
            return False
        self.put_json(f"jobs/{job_id}/outputs/{step_id}/_COMMITTED.json", {"worker": worker_id, "digest": digest})
        return True

    def fetch_output(self, job_id, step_id):
        ptr = self.get_json(f"jobs/{job_id}/outputs/{step_id}/_COMMITTED.json")
        if not ptr:
            raise FileNotFoundError(f"output non committato: {job_id}/{step_id}")
        return self._out(job_id, step_id) / ptr["worker"]

    def fetch_model(self, name, local):
        src, dst = self.root / "models_cache" / name, Path(local) / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst) if src.is_file() else shutil.copytree(src, dst)
        return dst


class S3Store(_Base):
    """Credenziali SOLO da variabili d'ambiente (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)."""

    def __init__(self, bucket, prefix="", endpoint_url=None, region=None, cache=os.path.join(tempfile.gettempdir(), "gpu_pool_fetch")):
        import boto3
        self.s3 = boto3.client("s3", endpoint_url=endpoint_url or None, region_name=region or None)
        self.bucket = bucket
        self.prefix = (prefix.strip("/") + "/") if prefix else ""
        self.cache = Path(cache)

    def _k(self, key):
        return self.prefix + key

    def put_json(self, key, obj):
        self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=json.dumps(obj).encode(),
                           ContentType="application/json")

    def get_json(self, key, default=None):
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read())
        except self.s3.exceptions.NoSuchKey:
            return default
        except self.s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return default
            raise

    def _list(self, prefix, delimiter=None):
        kw = {"Bucket": self.bucket, "Prefix": self._k(prefix)}
        if delimiter:
            kw["Delimiter"] = delimiter
        for page in self.s3.get_paginator("list_objects_v2").paginate(**kw):
            yield page

    def list_job_ids(self):
        ids = []
        for page in self._list("jobs/", "/"):
            for cp in page.get("CommonPrefixes", []):
                jid = cp["Prefix"][len(self._k("jobs/")):].strip("/")
                if self.get_json(f"jobs/{jid}/manifest.json"):
                    ids.append(jid)
        return ids

    def stage_output(self, tmp, job_id, step_id, worker_id):
        tmp = Path(tmp)
        digest = dir_digest(tmp)
        base = f"jobs/{job_id}/outputs/{step_id}/{worker_id}/"
        files = {}
        for p in sorted(tmp.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(tmp))
                self.s3.upload_file(str(p), self.bucket, self._k(base + rel))
                files[rel] = p.stat().st_size
        self.put_json(base + "_SUCCESS", {"digest": digest, "files": files})   # per ultimo
        shutil.rmtree(tmp, ignore_errors=True)
        return digest

    def finalize_output(self, job_id, step_id, worker_id, digest):
        base = f"jobs/{job_id}/outputs/{step_id}/{worker_id}/"
        marker = self.get_json(base + "_SUCCESS")
        if not marker or marker["digest"] != digest:
            return False
        for rel, size in marker["files"].items():       # controllo di presenza e dimensione
            try:
                if self.s3.head_object(Bucket=self.bucket, Key=self._k(base + rel))["ContentLength"] != size:
                    return False
            except self.s3.exceptions.ClientError:
                return False
        self.put_json(f"jobs/{job_id}/outputs/{step_id}/_COMMITTED.json", {"worker": worker_id, "digest": digest})
        return True

    def fetch_output(self, job_id, step_id):
        ptr = self.get_json(f"jobs/{job_id}/outputs/{step_id}/_COMMITTED.json")
        if not ptr:
            raise FileNotFoundError(f"output non committato: {job_id}/{step_id}")
        local = self.cache / job_id / step_id / ptr["digest"][:16]
        if (local / ".ok").exists():
            return local
        base = f"jobs/{job_id}/outputs/{step_id}/{ptr['worker']}/"
        marker = self.get_json(base + "_SUCCESS")
        for rel in marker["files"]:
            dst = local / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            self.s3.download_file(self.bucket, self._k(base + rel), str(dst))
        (local / ".ok").write_text("1")
        return local

    def fetch_model(self, name, local):
        dst = Path(local) / name
        if dst.exists():
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, self._k("models_cache/" + name), str(dst))
        return dst


StateStore = LocalStore


def open_store(cfg):
    if cfg.get("store_backend", "local") == "s3":
        return S3Store(cfg["s3_bucket"], cfg.get("s3_prefix", ""), cfg.get("s3_endpoint_url"), cfg.get("s3_region"))
    return LocalStore(cfg["store_root"])
