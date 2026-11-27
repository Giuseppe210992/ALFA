#!/usr/bin/env python3
"""Esegue `demo.py` di LingBot-Map (v1) SENZA interfaccia grafica e salva i risultati su file.

Non riscrive la logica del modello: carica il vero `demo.py` del repository e sostituisce
soltanto il visualizzatore finale (viser, che apre un server web) con un salvataggio:

    predictions.npz     pose (extrinsic/intrinsic), profondità, confidenza, world_points
    camera_poses.json   posizione e matrice camera->mondo di ogni fotogramma
    scene.glb           nuvola di punti + traiettoria camera (apribile in Blender, MeshLab, viewer web)
    summary.json        riepilogo (n. fotogrammi, forme degli array, errori non fatali)

Uso (lo lancia l'adapter, ma si può usare anche a mano):

    python lingbot_map_runner.py --repo /percorso/lingbot-map --out risultati/ -- \\
        --model_path lingbot-map.pt --image_folder foto/ --use_sdpa
"""
import argparse
import json
import os
import sys
import traceback
import types

KEEP = ("extrinsic", "intrinsic", "depth", "depth_conf", "world_points", "world_points_conf")


def _saver_class(out_dir, glb_percent, state):
    import numpy as np

    class HeadlessSaver:
        """Stesso costruttore di PointCloudViewer; `run()` salva invece di servire una pagina."""

        def __init__(self, pred_dict, **kwargs):
            self.pred = pred_dict
            self.kwargs = kwargs

        def run(self, *a, **k):
            try:
                self._save()
            except BaseException as e:      # mai ImportError: demo.main() lo scambierebbe per "viser mancante"
                traceback.print_exc()
                raise RuntimeError(f"salvataggio risultati fallito: {type(e).__name__}: {e}") from None
            state["saved"] = True

        def _save(self):
            pred = self.pred
            os.makedirs(out_dir, exist_ok=True)
            arrays = {k: np.asarray(pred[k]) for k in KEEP if pred.get(k) is not None}
            if "extrinsic" not in arrays:
                raise ValueError(f"il modello non ha prodotto 'extrinsic' (chiavi: {sorted(pred)})")
            np.savez_compressed(os.path.join(out_dir, "predictions.npz"), **arrays)

            # pose camera->mondo (il modello restituisce mondo->camera 3x4)
            ext = np.asarray(arrays["extrinsic"], dtype=np.float64)
            w2c = np.zeros((ext.shape[0], 4, 4))
            w2c[:, :3, :4] = ext
            w2c[:, 3, 3] = 1.0
            c2w = np.linalg.inv(w2c)
            poses = [{"frame": i, "position": c2w[i, :3, 3].tolist(), "camera_to_world": c2w[i].tolist()}
                     for i in range(len(c2w))]
            with open(os.path.join(out_dir, "camera_poses.json"), "w") as f:
                json.dump({"convention": "camera_to_world", "frames": poses}, f)

            summary = {"frames": int(ext.shape[0]),
                       "arrays": {k: list(v.shape) for k, v in arrays.items()},
                       "glb": False}
            try:
                from lingbot_map.vis.glb_export import predictions_to_glb
                scene = predictions_to_glb(pred, conf_thres=glb_percent, show_cam=True)
                scene.export(os.path.join(out_dir, "scene.glb"))
                summary["glb"] = True
            except BaseException as e:      # il GLB è un di più: le predizioni sono già salvate
                summary["glb_error"] = f"{type(e).__name__}: {e}"
                print(f"[runner] GLB non creato: {summary['glb_error']}", flush=True)
            with open(os.path.join(out_dir, "summary.json"), "w") as f:
                json.dump(summary, f, indent=1)
            print(f"[runner] risultati salvati in {out_dir} ({summary['frames']} fotogrammi, "
                  f"glb={summary['glb']})", flush=True)

    return HeadlessSaver


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="cartella del repository lingbot-map (contiene demo.py)")
    ap.add_argument("--out", required=True, help="cartella dove salvare i risultati")
    ap.add_argument("--glb-percent", type=float, default=50.0,
                    help="percentuale di punti a bassa confidenza scartati nel GLB (default 50)")
    ap.add_argument("demo_args", nargs=argparse.REMAINDER, help="argomenti per demo.py, dopo '--'")
    a = ap.parse_args(argv)
    demo_args = a.demo_args[1:] if a.demo_args[:1] == ["--"] else a.demo_args

    repo = os.path.abspath(a.repo)
    if not os.path.isfile(os.path.join(repo, "demo.py")):
        sys.exit(f"demo.py non trovato in {repo}")
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    sys.path.insert(0, repo)
    os.chdir(repo)          # demo.py cerca alcuni file (es. skyseg.onnx) relativi alla cartella corrente

    state = {"saved": False}
    # Pacchetto `lingbot_map.vis` "finto": non esegue il suo __init__ (che importa viser, non
    # necessario qui) ma lascia importabili i suoi sottomoduli reali, es. glb_export.
    vis = types.ModuleType("lingbot_map.vis")
    vis.__path__ = [os.path.join(repo, "lingbot_map", "vis")]
    vis.PointCloudViewer = _saver_class(out, a.glb_percent, state)
    sys.modules["lingbot_map.vis"] = vis

    import demo  # noqa: E402  (il demo.py del repository)
    sys.argv = ["demo.py"] + demo_args
    demo.main()
    if not state["saved"]:
        sys.exit("il modello non ha prodotto risultati (nessun salvataggio eseguito)")


if __name__ == "__main__":
    main()
