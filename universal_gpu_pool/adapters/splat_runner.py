#!/usr/bin/env python3
"""Video/foto -> Gaussian Splatting con COLMAP + nerfstudio (splatfacto). Gira su Kaggle dopo splat_setup.sh.

Passi: 1) sceglie i fotogrammi  2) COLMAP (ns-process-data)  3) addestra lo splat (ns-train)  4) esporta il .ply (ns-export).
Risultati in --out:  scene_splat.ply (si apre su superspl.at/editor), camera_poses.json, summary.json.
Solo libreria standard di Python: i programmi di nerfstudio si chiamano dall'ambiente isolato (--venv).
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

IMG_EXT = (".jpg", ".jpeg", ".png")


def log(msg):
    print("[splat] " + msg, flush=True)


def pick_frames(paths, n_max):
    """Al massimo n_max fotogrammi, distribuiti in modo uniforme (primo e ultimo inclusi)."""
    if n_max <= 0 or len(paths) <= n_max:
        return list(paths)
    if n_max == 1:
        return [paths[len(paths) // 2]]
    step = (len(paths) - 1) / float(n_max - 1)
    return [paths[int(round(i * step))] for i in range(n_max)]


def gpu_arch():
    """Architettura della GPU (es. 7.5 per la T4): rende molto più veloce l'eventuale compilazione di gsplat."""
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], text=True, timeout=20)
        return out.strip().splitlines()[0].strip()
    except Exception:
        return ""


def run(cmd, env, label):
    """Esegue un comando mostrando l'output in diretta; ritorna (codice, ultime righe)."""
    log("%s: %s" % (label, " ".join(cmd)))
    t0 = time.time()
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    tail = []
    for line in p.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        tail.append(line.rstrip())
        tail = tail[-30:]
    rc = p.wait()
    log("%s finito (codice %d) in %.0f s" % (label, rc, time.time() - t0))
    return rc, "\n".join(tail)


def run_variants(variants, env, label):
    """Prova più forme dello stesso comando (le opzioni cambiano tra versioni): passa alla successiva solo se l'opzione non esiste."""
    last = (1, "")
    for v in variants:
        rc, tail = run(v, env, label)
        if rc == 0:
            return rc, tail
        last = (rc, tail)
        low = tail.lower()
        if not ("no such option" in low or "unrecognized" in low or "unexpected extra" in low or "invalid choice" in low):
            break
    return last


def count_registered(proc_dir):
    try:
        with open(os.path.join(proc_dir, "transforms.json"), encoding="utf-8") as f:
            return len(json.load(f).get("frames", []))
    except Exception:
        return 0


def save_poses(proc_dir, out):
    try:
        with open(os.path.join(proc_dir, "transforms.json"), encoding="utf-8") as f:
            fr = json.load(f).get("frames", [])
        poses = [{"frame": i, "file": x.get("file_path", ""), "position": [row[3] for row in x["transform_matrix"][:3]],
                  "camera_to_world": x["transform_matrix"]} for i, x in enumerate(fr) if "transform_matrix" in x]
        with open(os.path.join(out, "camera_poses.json"), "w") as f:
            json.dump({"convention": "camera_to_world (nerfstudio / OpenGL)", "frames": poses}, f)
    except Exception as e:
        log("pose non salvate: %s" % e)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--venv", default="/tmp/nsvenv")
    ap.add_argument("--work", default="/tmp/gpool_splat")
    ap.add_argument("--max-frames", type=int, default=80)
    ap.add_argument("--iters", type=int, default=15000)
    ap.add_argument("--matching", default="sequential", choices=["sequential", "exhaustive"])
    a = ap.parse_args(argv)

    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    bindir = os.path.join(a.venv, "bin")
    env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1")
    arch = gpu_arch()
    if arch and "TORCH_CUDA_ARCH_LIST" not in env:
        env["TORCH_CUDA_ARCH_LIST"] = arch
    summary = {"stages": {}}

    paths = sorted(os.path.join(a.images, f) for f in os.listdir(a.images) if f.lower().endswith(IMG_EXT))
    if len(paths) < 8:
        sys.exit("servono almeno 8 immagini per il Gaussian Splatting (trovate %d): fai un video più lungo o più foto" % len(paths))
    used = pick_frames(paths, a.max_frames)
    frames_dir = os.path.join(a.work, "frames")
    shutil.rmtree(a.work, ignore_errors=True)
    os.makedirs(frames_dir)
    for i, p in enumerate(used):
        shutil.copy(p, os.path.join(frames_dir, "%05d%s" % (i + 1, os.path.splitext(p)[1].lower())))
    log("%d fotogrammi scelti su %d" % (len(used), len(paths)))
    summary.update(frames_available=len(paths), frames_used=len(used), iters=a.iters)

    # 1) COLMAP
    proc = os.path.join(a.work, "processed")
    t0 = time.time()
    base = [os.path.join(bindir, "ns-process-data"), "images", "--data", frames_dir, "--output-dir", proc]
    rc, tail = run_variants([base + ["--no-gpu", "--matching-method", a.matching], base + ["--no-gpu"], base], env,
                            "COLMAP (posizioni delle camere)")
    summary["stages"]["colmap_s"] = round(time.time() - t0)
    if rc != 0:
        sys.exit("COLMAP non è riuscito (codice %d). Ultime righe:\n%s" % (rc, tail))
    reg = count_registered(proc)
    summary["frames_registered"] = reg
    log("COLMAP ha allineato %d fotogrammi su %d" % (reg, len(used)))
    if reg < 8 or reg < 0.4 * len(used):
        sys.exit("COLMAP ha allineato solo %d fotogrammi su %d: il video ha movimento troppo veloce o pareti senza dettagli. "
                 "Riprova muovendoti più lentamente, con più sovrapposizione e luce uniforme." % (reg, len(used)))
    save_poses(proc, out)

    # 2) addestramento
    t0 = time.time()
    outputs = os.path.join(a.work, "outputs")
    train = [os.path.join(bindir, "ns-train"), "splatfacto", "--data", proc, "--output-dir", outputs,
             "--max-num-iterations", str(a.iters)]
    rc, tail = run_variants([train + ["--vis", "tensorboard"], train + ["--viewer.quit-on-train-completion", "True"], train],
                            env, "addestramento dello splat")
    summary["stages"]["train_s"] = round(time.time() - t0)
    if rc != 0:
        hint = ""
        if "out of memory" in tail.lower():
            hint = "\nLa memoria della GPU è finita: riprova con meno fotogrammi."
        if "nvcc" in tail.lower() or "ninja" in tail.lower():
            hint += "\nLa compilazione di gsplat è fallita (manca il compilatore CUDA)."
        sys.exit("addestramento non riuscito (codice %d). Ultime righe:\n%s%s" % (rc, tail, hint))
    cfgs = sorted(glob.glob(os.path.join(outputs, "**", "config.yml"), recursive=True), key=os.path.getmtime)
    if not cfgs:
        sys.exit("addestramento finito ma non trovo config.yml in %s" % outputs)

    # 3) esportazione
    t0 = time.time()
    exp = os.path.join(a.work, "exports")
    rc, tail = run([os.path.join(bindir, "ns-export"), "gaussian-splat", "--load-config", cfgs[-1], "--output-dir", exp], env,
                   "esportazione .ply")
    summary["stages"]["export_s"] = round(time.time() - t0)
    plys = sorted(glob.glob(os.path.join(exp, "**", "*.ply"), recursive=True), key=os.path.getsize)
    if rc != 0 or not plys:
        sys.exit("esportazione non riuscita (codice %d). Ultime righe:\n%s" % (rc, tail))
    dst = os.path.join(out, "scene_splat.ply")
    shutil.copy(plys[-1], dst)
    summary["ply_mb"] = round(os.path.getsize(dst) / 1e6, 1)
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    log("fatto: scene_splat.ply (%.1f MB). Aprilo su https://superspl.at/editor" % summary["ply_mb"])


if __name__ == "__main__":
    main()
