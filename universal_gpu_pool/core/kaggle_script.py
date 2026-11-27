"""Genera lo script Python che gira DENTRO il notebook Kaggle e fa tutto da solo:

    1. clona il repository GitHub           5. esegue il comando (con timeout)
    2. esegue i comandi di installazione    6. raccoglie i file prodotti in /kaggle/working/outputs
    3. scarica i file di input (link)       7. scrive result.json + run.log (sempre, anche in caso di errore)
    4. sostituisce {segnaposto} nel comando

Lo script usa SOLO la libreria standard: lo spazio è quello di Kaggle (Linux, root, internet attivo).
Variabili per i test locali: GPOOL_WORKDIR (al posto di /kaggle/working), GPOOL_TMPDIR (al posto di /tmp).
"""
import base64
import json

TEMPLATE = r'''# Generato da GPU Pool. Non modificare: viene ricreato a ogni esecuzione.
import base64, fnmatch, glob, json, os, re, shlex, shutil, signal, subprocess, sys, threading, time, traceback
import urllib.request
from urllib.parse import urlparse

CFG = json.loads(base64.b64decode("__CFG__").decode("utf-8"))
WORK = os.environ.get("GPOOL_WORKDIR", "/kaggle/working")
TMP = os.environ.get("GPOOL_TMPDIR", "/tmp")
OUT = os.path.join(WORK, "outputs")
REPO = os.path.join(TMP, "gpool_repo")
INP = os.path.join(TMP, "gpool_inputs")
for d in (WORK, OUT, INP):
    os.makedirs(d, exist_ok=True)
LOGF = open(os.path.join(WORK, "run.log"), "a", buffering=1, encoding="utf-8")
T0 = time.time()
MAX_COPY_TOTAL = 3_000_000_000
MAX_COPY_FILE = 1_500_000_000
HEAVY_EXT = (".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".onnx", ".gguf", ".h5")
SKIP_PARTS = {".git", "__pycache__", "node_modules", ".ipynb_checkpoints", ".cache", "wandb"}


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    LOGF.write(line + "\n")


def run_sh(cmd, cwd, timeout, env):
    """Esegue un comando shell, ne inoltra l'output nel log e lo interrompe se supera il tempo."""
    log("$ " + cmd)
    p = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", bufsize=1, start_new_session=True)
    timed_out = []

    def killer():
        timed_out.append(True)
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
    timer = threading.Timer(timeout, killer)
    timer.start()
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            print(line, flush=True)
            LOGF.write(line + "\n")
    finally:
        timer.cancel()
        p.wait()
    if timed_out:
        log("!! interrotto: tempo massimo superato (%d s)" % timeout)
        return 124
    return p.returncode


def render(template, values):
    vals = dict(values)
    vals.update({"OUT": OUT, "REPO": REPO, "INPUTS": INP})
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
                  lambda m: shlex.quote(str(vals[m.group(1)])) if m.group(1) in vals else m.group(0), template)


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "gpu-pool/1.1"})
    n = 0
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            n += len(b)
            if n > 3_000_000_000:
                raise RuntimeError("file di input oltre 3 GB")
            f.write(b)
    return n


def snapshot(root):
    snap = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_PARTS]
        for f in fn:
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
                snap[os.path.relpath(p, root)] = (st.st_mtime, st.st_size)
            except OSError:
                pass
    return snap


def copy_new(before, extra_globs):
    """Copia in OUT/ i file creati o modificati dal programma dentro il repository."""
    total, copied = 0, []
    after = snapshot(REPO)
    wanted = set()
    for rel, meta in after.items():
        if rel not in before or before[rel] != meta:
            wanted.add(rel)
    for pat in extra_globs:
        for m in glob.glob(os.path.join(REPO, pat), recursive=True):
            if os.path.isfile(m):
                wanted.add(os.path.relpath(m, REPO))
            elif os.path.isdir(m):
                for dp, dn, fn in os.walk(m):
                    dn[:] = [d for d in dn if d not in SKIP_PARTS]
                    for f in fn:
                        wanted.add(os.path.relpath(os.path.join(dp, f), REPO))
    for rel in sorted(wanted):
        src = os.path.join(REPO, rel)
        low = rel.lower()
        if low.endswith((".pyc", ".pyo")) or any(part in SKIP_PARTS for part in rel.split(os.sep)):
            continue
        try:
            size = os.path.getsize(src)
        except OSError:
            continue
        if size > MAX_COPY_FILE or (low.endswith(HEAVY_EXT) and size > 200_000_000):
            log("salto (troppo grande): %s (%.0f MB)" % (rel, size / 1e6))
            continue
        if total + size > MAX_COPY_TOTAL:
            log("limite di dimensione raggiunto: altri file non copiati")
            break
        dst = os.path.join(OUT, "dal_repository", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        total += size
        copied.append(rel)
    return copied


def list_outputs():
    files = []
    for dp, dn, fn in os.walk(OUT):
        for f in fn:
            p = os.path.join(dp, f)
            files.append({"name": os.path.relpath(p, WORK).replace(os.sep, "/"), "size": os.path.getsize(p)})
    return sorted(files, key=lambda x: x["name"])


def write_result(res):
    res["seconds"] = round(time.time() - T0, 1)
    with open(os.path.join(WORK, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)


def main():
    res = {"ok": False, "stage": "start", "exit_code": None, "error": "", "repo": CFG["repo_url"]}
    env = dict(os.environ, PYTHONUNBUFFERED="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1",
               DEBIAN_FRONTEND="noninteractive", GPOOL_OUT=OUT, GIT_TERMINAL_PROMPT="0", TOKENIZERS_PARALLELISM="false")
    try:
        log("GPU Pool: avvio %s" % CFG["repo_url"])
        run_sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || echo 'nessuna GPU visibile'", WORK, 60, env)

        # 1. clone
        res["stage"] = "clone"
        shutil.rmtree(REPO, ignore_errors=True)
        ref = CFG.get("ref") or ""
        cmd = "git clone --depth 1 %s %s %s" % (("--branch " + shlex.quote(ref)) if ref else "",
                                                shlex.quote(CFG["clone_url"]), shlex.quote(REPO))
        rc = run_sh(cmd, TMP, 900, env)
        if rc != 0 and ref:     # forse è un commit, non un ramo
            shutil.rmtree(REPO, ignore_errors=True)
            rc = run_sh("git clone %s %s && cd %s && git checkout %s" % (shlex.quote(CFG["clone_url"]), shlex.quote(REPO),
                                                                          shlex.quote(REPO), shlex.quote(ref)), TMP, 900, env)
        if rc != 0:
            raise RuntimeError("impossibile scaricare il progetto da GitHub (link o ramo errati?)")
        try:
            res["commit"] = subprocess.check_output(["git", "-C", REPO, "rev-parse", "HEAD"], text=True).strip()
        except Exception:
            pass

        for rel, content in (CFG.get("files") or {}).items():       # file forniti dal sistema (es. script di avvio)
            dst = os.path.join(REPO, rel)
            if os.path.commonpath([os.path.realpath(REPO), os.path.realpath(dst)]) != os.path.realpath(REPO):
                raise RuntimeError("percorso file non valido: " + rel)
            os.makedirs(os.path.dirname(dst) or REPO, exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(content)

        # 2. setup
        res["stage"] = "setup"
        for c in CFG.get("setup", []):
            rc = run_sh(render(c, {}), REPO, CFG.get("setup_timeout_s", 2400), env)
            if rc != 0:
                raise RuntimeError("l'installazione è fallita (comando: %s)" % c[:120])

        # 3. input
        res["stage"] = "inputs"
        values = {}
        for k, v in CFG.get("values", {}).items():
            values[k] = v
        for k, url in CFG.get("downloads", {}).items():
            ext = os.path.splitext(urlparse(url).path)[1][:8]
            if not re.match(r"^\.[A-Za-z0-9]+$", ext or ""):
                ext = ""
            dest = os.path.join(INP, k + ext)
            log("scarico l'input «%s» da %s" % (k, url))
            n = fetch(url, dest)
            log("  %.1f MB" % (n / 1e6))
            if ext.lower() == ".zip":      # una cartella di foto: si passa la cartella estratta
                import zipfile
                d = os.path.join(INP, k)
                with zipfile.ZipFile(dest) as z:
                    for m in z.infolist():
                        t = os.path.realpath(os.path.join(d, m.filename))
                        if os.path.commonpath([os.path.realpath(d), t]) != os.path.realpath(d):
                            raise RuntimeError("ZIP non sicuro")
                    z.extractall(d)
                subs = [x for x in os.listdir(d) if not x.startswith("__MACOSX")]
                dest = os.path.join(d, subs[0]) if len(subs) == 1 and os.path.isdir(os.path.join(d, subs[0])) else d
            values[k] = dest

        # 4-5. esecuzione
        res["stage"] = "run"
        before = snapshot(REPO)
        cmd = render(CFG["run"], values)
        rc = run_sh(cmd, REPO, CFG.get("timeout_s", 7200), env)
        res["exit_code"] = rc

        # 6. risultati
        res["stage"] = "collect"
        copied = copy_new(before, CFG.get("outputs", []))
        res["copied_from_repo"] = len(copied)
        res["files"] = list_outputs()
        if rc == 124:
            res["error"] = "tempo massimo superato"
        elif rc != 0:
            res["error"] = "il programma è terminato con errore (codice %s)" % rc
        elif not res["files"]:
            res["error"] = ""
            log("nota: il programma è finito ma non ha prodotto file.")
        res["ok"] = (rc == 0)
    except BaseException as e:
        log("ERRORE: %s" % e)
        log(traceback.format_exc())
        res["error"] = str(e)
        try:
            res["files"] = list_outputs()
        except Exception:
            pass
    finally:
        res["stage"] = res.get("stage", "")
        log("fine: ok=%s" % res["ok"])
        write_result(res)


main()
'''


def build_script(cfg):
    """cfg: repo_url, clone_url, ref, setup[], run, values{}, downloads{}, outputs[], timeout_s, setup_timeout_s."""
    blob = base64.b64encode(json.dumps(cfg, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return TEMPLATE.replace("__CFG__", blob)
