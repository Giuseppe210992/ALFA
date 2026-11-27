"""Adapter LingBot-Map (v1): ricostruzione 3D in streaming da video o foto.

Modello: https://github.com/Robbyant/lingbot-map  (Apache-2.0, pesi pubblici su Hugging Face).
Richiede un nodo con GPU NVIDIA + PyTorch con CUDA (Kaggle/Colab vanno bene): sul tuo PC senza
GPU il job resta in coda, mai eseguito dal worker locale "solo API".

Un item = una scena = uno step del job:
    {"id": "casa", "video": "https://.../video.mp4"}           video (URL o percorso sul nodo)
    {"id": "casa", "images": "https://.../foto.zip"}           ZIP di foto (URL) o cartella sul nodo

Parametri del job (params.options, tutti opzionali):
    fps (10)  stride (1)  first_k       campionamento del video / delle foto
    mode ("streaming" | "windowed")     windowed per sequenze molto lunghe (> ~3000 fotogrammi)
    keyframe_interval  num_scale_frames  camera_num_iterations  window_size  overlap_keyframes
    checkpoint ("lingbot-map.pt" | "lingbot-map-long.pt")   hf_repo ("robbyant/lingbot-map")
    use_sdpa (auto: True se FlashInfer non è installato)     compile (False)
    glb_percent (50)                    % di punti meno affidabili scartati dal file .glb
    repo_dir / model_path               usa un repository o dei pesi già presenti sul nodo
    auto_install (True)                 clona il repo e installa le librerie leggere mancanti
    require_gpu (True)                  fallisce subito se il nodo non ha una GPU CUDA
    check_deps (True)                   verifica/installa PyTorch e le librerie (False solo nei test)
    timeout_s (21600)

Output per scena (nello storage del pool): scene.glb, predictions.npz, camera_poses.json,
summary.json, run.log, results.jsonl.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from adapters.base_adapter import BaseAdapter
from core import uploads
from core.linkfix import direct_url

REPO_URL = "https://github.com/Robbyant/lingbot-map.git"
RUNNER = Path(__file__).with_name("lingbot_map_runner.py")
VIDEO_EXT = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")
# modulo python -> pacchetto pip. torch NON si installa da solo (è enorme e dipende dalla CUDA del nodo).
LIGHT_DEPS = {"cv2": "opencv-python-headless", "PIL": "Pillow", "einops": "einops",
              "huggingface_hub": "huggingface_hub", "safetensors": "safetensors", "tqdm": "tqdm",
              "scipy": "scipy", "trimesh": "trimesh", "matplotlib": "matplotlib", "numpy": "numpy"}


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_url(s):
    return str(s).lower().startswith(("http://", "https://"))


def _safe_id(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s))[:60] or "scena"


def _download(url, dest, max_bytes, log):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "gpu-pool/1.0"})
    n = 0
    host = (urlparse(url).hostname or "").lower()
    opener = _DIRECT.open if host in ("127.0.0.1", "localhost", "::1") else urllib.request.urlopen
    with opener(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > max_bytes:
                raise RuntimeError(f"download oltre il limite di {max_bytes / 1e9:.1f} GB: {url}")
            f.write(chunk)
    tmp.replace(dest)
    log(f"scaricato {url} ({n / 1e6:.1f} MB)")
    return dest


def _safe_unzip(zip_path, dest):
    """Estrae uno ZIP rifiutando percorsi che escono dalla cartella di destinazione."""
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            target = (dest / m.filename).resolve()
            if os.path.commonpath([str(dest), str(target)]) != str(dest):
                raise RuntimeError(f"ZIP non sicuro (percorso fuori cartella): {m.filename}")
        z.extractall(dest)
    return dest


def _image_root(folder):
    """Se lo ZIP contiene una sola cartella di foto, scende in quella."""
    folder = Path(folder)
    exts = (".jpg", ".jpeg", ".png")
    best, best_n = folder, 0
    for d in [folder] + [p for p in folder.rglob("*") if p.is_dir() and "__MACOSX" not in p.parts]:
        n = sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts)
        if n > best_n:
            best, best_n = d, n
    return best, best_n


class LingBotMapAdapter(BaseAdapter):
    name = "lingbot_map"

    # ---------- piano ----------
    def plan(self, items, params, limits):
        if params.get("runtime") == "kaggle":
            return self._plan_kaggle(items, params)
        steps = []
        for i, it in enumerate(items):
            if isinstance(it, str):
                key = "video" if (it.lower().split("?")[0].endswith(VIDEO_EXT)) else "images"
                it = {key: it}
            if not isinstance(it, dict):
                raise ValueError(f"item {i}: atteso testo o oggetto, trovato {type(it).__name__}")
            if bool(it.get("video")) == bool(it.get("images")):
                raise ValueError(f"item {i}: indica esattamente uno tra 'video' e 'images'")
            it = dict(it)
            it.setdefault("id", f"scena{i + 1}")
            steps.append({"id": f"step_{i + 1:03d}", "depends_on": [], "payload": {"scene": it}})
        return steps

    def _plan_kaggle(self, items, params):
        """runtime «kaggle»: stessa ricostruzione, ma eseguita su una GPU gratuita di Kaggle (nessun nodo del pool)."""
        from core import github_repo
        from core.recipes import LINGBOT_REPO, lingbot_cfg
        opts = dict(params.get("options") or {})
        owner, repo, _ = github_repo.parse_url(LINGBOT_REPO)
        steps = []
        for i, it in enumerate(items):
            if isinstance(it, str):
                it = {("video" if it.lower().split("?")[0].endswith(VIDEO_EXT) else "images"): it}
            if not isinstance(it, dict) or bool(it.get("video")) == bool(it.get("images")):
                raise ValueError(f"item {i}: indica esattamente uno tra 'video' e 'images'")
            key = "video" if it.get("video") else "images"
            url = str(it[key]).strip()
            up = {}
            uid = uploads.parse_ref(url)
            if uid:
                path = uploads.path_for(uid)
                if not path:
                    raise ValueError("il file caricato non è più disponibile: caricalo di nuovo")
                up = {key: {"id": uid, "name": path.name}}
            elif not _is_url(url):
                raise ValueError("per l'esecuzione su Kaggle serve un link (https://…) oppure un file trascinato dal PC")
            else:
                try:
                    url = direct_url(url)[0]
                except ValueError as e:
                    raise ValueError(str(e))
            rc = lingbot_cfg({key: url}, opts)
            cfg = {"repo_url": f"https://github.com/{owner}/{repo}", "clone_url": github_repo.clone_url(owner, repo),
                   "ref": opts.get("repo_ref", ""), "setup": rc["setup"], "run": rc["run"], "values": {},
                   "downloads": ({} if up else {rc["input_key"]: url}), "outputs": [], "timeout_s": rc["timeout_s"],
                   "setup_timeout_s": 2400, "gpu": True, "title": rc["title"], "files": rc["files"], "uploads": up}
            steps.append({"id": f"step_{i + 1:03d}", "depends_on": [], "payload": {"cfg": cfg}})
        if not steps:
            raise ValueError("indica un video o uno ZIP di foto")
        return steps

    # ---------- ambiente ----------
    @staticmethod
    def _run(cmd, log, timeout=1800, **kw):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        if r.returncode:
            tail = (r.stdout + r.stderr).strip().splitlines()[-8:]
            raise RuntimeError(f"comando fallito ({' '.join(map(str, cmd[:3]))}…): " + " | ".join(tail))
        return r

    def _ensure_repo(self, opts, cache, log):
        repo = opts.get("repo_dir") or os.environ.get("LINGBOT_MAP_DIR")
        if repo:
            if not (Path(repo) / "demo.py").is_file():
                raise RuntimeError(f"repo_dir non valido (manca demo.py): {repo}")
            return Path(repo)
        repo = cache / "lingbot-map"
        if (repo / "demo.py").is_file():
            return repo
        if not opts.get("auto_install", True):
            raise RuntimeError("repository LingBot-Map assente e auto_install disattivato")
        if not shutil.which("git"):
            raise RuntimeError("git non trovato sul nodo: impossibile scaricare LingBot-Map")
        log(f"clono {REPO_URL}")
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        cmd = ["git", "clone", "--depth", "1"]
        if opts.get("repo_ref"):
            cmd += ["--branch", str(opts["repo_ref"])]
        self._run(cmd + [REPO_URL, str(repo)], log, timeout=900)
        return repo

    def _ensure_deps(self, opts, log):
        if not opts.get("check_deps", True):
            return
        if importlib.util.find_spec("torch") is None:
            raise RuntimeError("PyTorch non è installato su questo nodo (serve la versione con CUDA).")
        missing = [pip for mod, pip in LIGHT_DEPS.items() if importlib.util.find_spec(mod) is None]
        if missing:
            if not opts.get("auto_install", True):
                raise RuntimeError("librerie mancanti: " + ", ".join(missing))
            log("installo: " + " ".join(missing))
            self._run([sys.executable, "-m", "pip", "install", "-q"] + missing, log, timeout=1800)

    def _ensure_weights(self, opts, cache, log):
        mp = opts.get("model_path")
        if mp:
            if not Path(mp).is_file():
                raise RuntimeError(f"model_path non trovato: {mp}")
            return Path(mp)
        fname = opts.get("checkpoint", "lingbot-map.pt")
        dest = cache / "weights" / fname
        if dest.is_file():
            return dest
        from huggingface_hub import hf_hub_download
        log(f"scarico i pesi {fname} da Hugging Face (~4,6 GB, solo la prima volta)")
        p = hf_hub_download(repo_id=opts.get("hf_repo", "robbyant/lingbot-map"), filename=fname,
                            local_dir=str(cache / "weights"))
        return Path(p)

    def _require_gpu(self, opts):
        if not opts.get("require_gpu", True):
            return
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("questo nodo non ha una GPU CUDA: LingBot-Map richiede una GPU NVIDIA")

    # ---------- input ----------
    def _prepare_input(self, scene, work, opts, log):
        max_bytes = int(float(opts.get("max_download_gb", 5)) * 1e9)
        if scene.get("video"):
            src = str(scene["video"])
            if _is_url(src):
                src = direct_url(src)[0]
                name = Path(src.split("?")[0]).name or "video.mp4"
                src = str(_download(src, work / name, max_bytes, log))
            if not Path(src).is_file():
                raise RuntimeError(f"video non trovato: {src}")
            return ["--video_path", src, "--fps", str(int(opts.get("fps", 10)))]
        src = str(scene["images"])
        if _is_url(src):
            src = direct_url(src)[0]
            z = _download(src, work / "foto.zip", max_bytes, log)
            src = str(_safe_unzip(z, work / "foto"))
        if not Path(src).is_dir():
            raise RuntimeError(f"cartella foto non trovata: {src}")
        root, n = _image_root(src)
        if n < 2:
            raise RuntimeError(f"servono almeno 2 foto (jpg/png), trovate {n} in {src}")
        log(f"{n} foto in {root}")
        return ["--image_folder", str(root)]

    # ---------- esecuzione ----------
    def run_step(self, ctx):
        if "cfg" in ctx.step["payload"]:                      # runtime kaggle: lo esegue l'adapter github_repo
            from adapters.github_repo_adapter import GitHubRepoAdapter
            ad = GitHubRepoAdapter()
            ad.run_step(ctx)
            self.last_metrics = getattr(ad, "last_metrics", None)
            return
        params = dict(ctx.params)
        opts = dict(params.get("options") or {})
        scene = ctx.step["payload"]["scene"]
        sid = _safe_id(scene.get("id"))
        cache = Path(getattr(ctx, "models_cache", None) or Path(tempfile.gettempdir()) / "gpu_pool_cache") / "lingbot_map"
        cache.mkdir(parents=True, exist_ok=True)
        out = Path(ctx.tmp_dir)
        log_lines = []

        def log(m):
            log_lines.append(f"[{time.strftime('%H:%M:%S')}] {m}")
            print(f"[lingbot_map] {m}", flush=True)

        t0 = time.time()
        self._require_gpu(opts)
        self._ensure_deps(opts, log)
        ctx.progress(0.03)
        repo = self._ensure_repo(opts, cache, log)
        ctx.progress(0.08)
        weights = self._ensure_weights(opts, cache, log)
        ctx.progress(0.15)

        work = Path(tempfile.mkdtemp(prefix=f"lingbot_{sid}_"))
        try:
            input_args = self._prepare_input(scene, work, opts, log)
            ctx.progress(0.2)
            use_sdpa = opts.get("use_sdpa")
            if use_sdpa is None:
                use_sdpa = importlib.util.find_spec("flashinfer") is None
            mode = opts.get("mode", "streaming")
            if mode not in ("streaming", "windowed"):
                raise ValueError(f"mode non valido: {mode!r}")
            args = ["--model_path", str(weights), "--mode", mode] + input_args
            for key, flag in (("stride", "--stride"), ("first_k", "--first_k"),
                              ("keyframe_interval", "--keyframe_interval"),
                              ("num_scale_frames", "--num_scale_frames"),
                              ("camera_num_iterations", "--camera_num_iterations"),
                              ("window_size", "--window_size"), ("overlap_keyframes", "--overlap_keyframes")):
                if opts.get(key) is not None:
                    args += [flag, str(int(opts[key]))]
            if use_sdpa:
                args.append("--use_sdpa")
            if opts.get("compile"):
                args.append("--compile")
            args.append("--offload_to_cpu" if opts.get("offload_to_cpu", True) else "--no-offload_to_cpu")

            cmd = [sys.executable, "-u", str(RUNNER), "--repo", str(repo), "--out", str(out),
                   "--glb-percent", str(float(opts.get("glb_percent", 50))), "--"] + args
            log("avvio: " + " ".join(cmd[3:]))
            self._stream(cmd, ctx, log_lines, timeout=int(opts.get("timeout_s", 21600)))
        finally:
            shutil.rmtree(work, ignore_errors=True)

        summary = {}
        try:
            summary = json.loads((out / "summary.json").read_text())
        except Exception:
            pass
        files = sorted(p.name for p in out.iterdir() if p.is_file() and not p.name.startswith("_")
                       and p.name != "results.jsonl")
        n = summary.get("frames", "?")
        msg = f"Ricostruzione 3D completata: {n} fotogrammi in {time.time() - t0:.0f} s."
        if summary.get("glb"):
            msg += " Scarica scene.glb (nuvola di punti + traiettoria) e camera_poses.json."
        elif summary.get("glb_error"):
            msg += f" (GLB non creato: {summary['glb_error']}; predictions.npz è disponibile.)"
        (out / "run.log").write_text("\n".join(log_lines[-400:]), encoding="utf-8")
        files.append("run.log")
        rec = {"id": scene.get("id"), "ok": True, "text": msg, "frames": n, "files": files,
               "seconds": round(time.time() - t0, 1)}
        with open(out / "results.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        ctx.progress(1.0)
        self.last_metrics = {"ok": 1, "failed": 0}

    def _stream(self, cmd, ctx, log_lines, timeout):
        """Esegue il runner, inoltra l'output e stima l'avanzamento dalle righe di demo.py."""
        marks = (("Building model", 0.25), ("Checkpoint loaded", 0.3), ("Input:", 0.35),
                 ("inference...", 0.4), ("Inference done", 0.85), ("Moving results", 0.88),
                 ("Building GLB", 0.93), ("risultati salvati", 0.97))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
        deadline = time.time() + timeout
        tail = []
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                tail.append(line)
                tail = tail[-60:]
                log_lines.append(line[:300])
                for text, p in marks:
                    if text in line:
                        ctx.progress(p)
                if time.time() > deadline:
                    proc.kill()
                    raise TimeoutError(f"LingBot-Map oltre il tempo massimo di {timeout} s")
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if rc != 0:
            important = [l for l in tail if re.search(r"Error|error|Traceback|CUDA|memory|Killed", l)] or tail
            hint = ""
            joined = "\n".join(tail)
            if "out of memory" in joined.lower():
                hint = " Suggerimento: riduci num_scale_frames a 2, usa keyframe_interval o mode 'windowed'."
            raise RuntimeError(f"LingBot-Map terminato con errore {rc}: " + " | ".join(important[-6:])[:1500] + hint)

    def validate(self, ctx):
        out = Path(ctx.tmp_dir)
        if not (out / "results.jsonl").exists():
            return False
        if "cfg" in ctx.step.get("payload", {}):              # esecuzione su Kaggle: anche un errore va consegnato
            return True
        return (out / "predictions.npz").exists()
