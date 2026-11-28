#!/usr/bin/env python3
"""Esegue Depth Anything 3 (DA3) su una cartella di fotogrammi e salva i risultati su file.

    scene.glb           nuvola di punti colorata + posizioni della fotocamera (Blender, MeshLab, viewer web)
    camera_poses.json   posizione e matrice camera->mondo di ogni fotogramma
    predictions.npz     profondità, confidenza, estrinseci e intrinseci
    summary.json        riepilogo (modello, fotogrammi usati, tentativi)

Se la memoria della GPU finisce, riprova da solo con la metà dei fotogrammi (fino a 3 tentativi).

Uso (lo lancia la ricetta, ma funziona anche a mano):
    python da3_runner.py --repo /percorso/Depth-Anything-3 --images foto/ --out risultati/ --model large
"""
import argparse
import importlib.abc
import importlib.machinery
import json
import math
import os
import sys
import traceback
import types

MODELS = {"large": "depth-anything/DA3-LARGE-1.1", "base": "depth-anything/DA3-BASE", "small": "depth-anything/DA3-SMALL"}
OPTIONAL = ("open3d", "moviepy", "evo", "e3nn", "pycolmap", "plyfile", "pillow_heif", "gsplat", "fastapi", "uvicorn", "typer")
IMG_EXT = (".jpg", ".jpeg", ".png")


class _Missing(object):
    """Segnaposto per librerie facoltative non installate: importarle non dà errore, usarle sì (con un messaggio chiaro)."""
    _name = "?"

    def __init__(self, *a, **k):
        raise ImportError("la libreria facoltativa «%s» non è installata (non serve per questo lavoro)" % self._name)


class _MissingModule(types.ModuleType):
    __path__ = []

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return type(item, (_Missing,), {"_name": self.__name__})


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, roots):
        self.roots = set(roots)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.roots:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        return _MissingModule(spec.name)

    def exec_module(self, module):
        pass


def install_shims():
    """DA3 importa alcune librerie solo per funzioni che qui non servono (video 3DGS, COLMAP…): se mancano non è un problema."""
    import numpy
    if not hasattr(numpy, "math"):
        numpy.math = math                       # numpy 2: «from numpy import math» non esiste più
        sys.modules.setdefault("numpy.math", math)
    missing = []
    for name in OPTIONAL:
        try:
            __import__(name)
        except Exception:
            missing.append(name)
    try:                                        # xformers non c'è su Kaggle: serve solo ai modelli «giant» (SwiGLU)
        import xformers.ops  # noqa: F401
    except Exception:
        import torch.nn as nn
        fake = types.ModuleType("xformers")
        ops = types.ModuleType("xformers.ops")

        class _Fake(nn.Module):
            def __init__(self, *a, **k):
                super().__init__()

            def forward(self, x):
                return x

        ops.SwiGLU = _Fake
        ops.MemoryEfficientAttention = _Fake
        ops.flash_attention = lambda *a, **k: None
        ops.unbind = lambda *a, **k: None
        fake.ops = ops
        sys.modules["xformers"] = fake
        sys.modules["xformers.ops"] = ops
    if missing:
        sys.meta_path.append(_Finder(missing))
    return missing


def pick_frames(paths, n_max):
    """Al massimo n_max fotogrammi, distribuiti in modo uniforme (si mantiene sempre il primo e l'ultimo)."""
    if n_max <= 0 or len(paths) <= n_max:
        return list(paths)
    if n_max == 1:
        return [paths[len(paths) // 2]]
    step = (len(paths) - 1) / float(n_max - 1)
    return [paths[int(round(i * step))] for i in range(n_max)]


def is_oom(exc):
    return "out of memory" in ("%s %s" % (type(exc).__name__, exc)).lower()


def save_poses(out, extrinsics, intrinsics, depth, conf):
    import numpy as np
    ext = np.asarray(extrinsics, dtype=np.float64)
    w2c = np.zeros((ext.shape[0], 4, 4))
    w2c[:, :3, :4] = ext[:, :3, :4]
    w2c[:, 3, 3] = 1.0
    c2w = np.linalg.inv(w2c)
    poses = [{"frame": i, "position": c2w[i, :3, 3].tolist(), "camera_to_world": c2w[i].tolist()} for i in range(len(c2w))]
    with open(os.path.join(out, "camera_poses.json"), "w") as f:
        json.dump({"convention": "camera_to_world", "frames": poses}, f)
    arrays = {"depth": np.asarray(depth), "extrinsic": ext, "intrinsic": np.asarray(intrinsics)}
    if conf is not None:
        arrays["conf"] = np.asarray(conf)
    np.savez_compressed(os.path.join(out, "predictions.npz"), **arrays)
    return {k: list(v.shape) for k, v in arrays.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="cartella del repository Depth-Anything-3")
    ap.add_argument("--images", required=True, help="cartella con i fotogrammi (jpg/png)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="large", choices=sorted(MODELS) + ["custom"])
    ap.add_argument("--model-id", default="", help="id Hugging Face, con --model custom")
    ap.add_argument("--max-frames", type=int, default=80)
    ap.add_argument("--process-res", type=int, default=504)
    ap.add_argument("--conf-percentile", type=float, default=40.0, help="percentuale di punti meno sicuri scartati nel GLB")
    ap.add_argument("--max-points", type=int, default=1000000)
    a = ap.parse_args(argv)

    repo = os.path.abspath(a.repo)
    src = os.path.join(repo, "src")
    if not os.path.isdir(os.path.join(src, "depth_anything_3")):
        sys.exit("Depth-Anything-3 non trovato in %s (manca src/depth_anything_3)" % repo)
    sys.path.insert(0, src)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    paths = sorted(os.path.join(a.images, f) for f in os.listdir(a.images) if f.lower().endswith(IMG_EXT))
    if len(paths) < 2:
        sys.exit("servono almeno 2 immagini in %s (trovate %d)" % (a.images, len(paths)))

    missing = install_shims()
    if missing:
        print("[da3] librerie facoltative assenti (ok): %s" % ", ".join(missing), flush=True)
    import torch
    from depth_anything_3.api import DepthAnything3

    model_id = a.model_id if a.model == "custom" else MODELS[a.model]
    print("[da3] carico il modello %s…" % model_id, flush=True)
    model = DepthAnything3.from_pretrained(model_id)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(dev)
    print("[da3] modello pronto su %s; %d fotogrammi disponibili" % (dev, len(paths)), flush=True)

    glb_dir = os.path.join(out, "_glb")
    pred, used, attempts = None, [], 0
    n_try = min(a.max_frames, len(paths))
    while True:
        used = pick_frames(paths, n_try)
        attempts += 1
        try:
            print("[da3] tentativo %d: %d fotogrammi" % (attempts, len(used)), flush=True)
            pred = model.inference(used, process_res=a.process_res, ref_view_strategy="middle",
                                   export_dir=glb_dir, export_format="glb",
                                   conf_thresh_percentile=a.conf_percentile, num_max_points=a.max_points, show_cameras=True)
            break
        except Exception as e:
            if not is_oom(e) or n_try <= 8 or attempts >= 4:
                raise
            print("[da3] memoria GPU esaurita con %d fotogrammi: riprovo con la metà" % len(used), flush=True)
            pred = None
            try:
                import gc
                gc.collect()
                torch.cuda.empty_cache()
            except Exception:
                pass
            n_try = max(8, n_try // 2)

    summary = {"model": model_id, "frames_used": len(used), "frames_available": len(paths), "attempts": attempts, "glb": False}
    try:
        summary["arrays"] = save_poses(out, pred.extrinsics, pred.intrinsics, pred.depth, getattr(pred, "conf", None))
    except Exception as e:
        traceback.print_exc()
        summary["poses_error"] = "%s: %s" % (type(e).__name__, e)
    for root, _d, files in os.walk(glb_dir):
        for fn in files:
            if fn.lower().endswith(".glb"):
                os.replace(os.path.join(root, fn), os.path.join(out, "scene.glb"))
                summary["glb"] = True
    if not summary["glb"]:
        summary["glb_error"] = "il modello non ha prodotto un file .glb"
        print("[da3] GLB non creato", flush=True)
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    if not summary["glb"] and "poses_error" in summary:
        sys.exit("nessun risultato salvato: %s" % summary["poses_error"])
    print("[da3] fatto: %d fotogrammi, glb=%s" % (len(used), summary["glb"]), flush=True)


if __name__ == "__main__":
    main()
