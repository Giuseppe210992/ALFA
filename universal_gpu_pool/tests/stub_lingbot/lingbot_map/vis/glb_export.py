"""Versione di prova di glb_export: crea un vero GLB con trimesh se disponibile."""
import numpy as np


def predictions_to_glb(predictions, conf_thres=50.0, show_cam=True, **kw):
    import trimesh  # ImportError se manca: il runner deve segnalarlo senza perdere le predizioni
    pts = np.asarray(predictions["depth"]).reshape(-1, 1) * np.ones((1, 3))
    scene = trimesh.Scene()
    scene.add_geometry(trimesh.PointCloud(vertices=pts[:500]))
    return scene
