"""Depth Anything 3 finto: stessa interfaccia usata da da3_runner (from_pretrained, to, inference)."""
import json
import os
import types

import numpy as np
import open3d                              # librerie facoltative assenti: il runner deve lasciarle importare
from moviepy.editor import ImageSequenceClip  # noqa: F401
from evo.core import trajectory  # noqa: F401

CALLS = os.environ.get("GPOOL_STUB_CALLS")


class DepthAnything3(object):
    @classmethod
    def from_pretrained(cls, model_id):
        o = cls()
        o.model_id = model_id
        return o

    def to(self, device):
        return self

    def inference(self, image, process_res=504, ref_view_strategy="x", export_dir=None, export_format="mini_npz",
                  conf_thresh_percentile=40.0, num_max_points=1000000, show_cameras=True, **kw):
        n = len(image)
        if CALLS:
            with open(CALLS, "a") as f:
                f.write(json.dumps({"n": n, "model": self.model_id, "res": process_res, "ref": ref_view_strategy,
                                    "conf": conf_thresh_percentile, "max_points": num_max_points}) + "\n")
        if n > int(os.environ.get("GPOOL_STUB_MAX", "10000")):
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        ext = np.zeros((n, 3, 4), dtype=np.float32)
        for i in range(n):
            ext[i, :3, :3] = np.eye(3)
            ext[i, 0, 3] = float(i)
        pred = types.SimpleNamespace(depth=np.ones((n, 4, 4), np.float32), conf=np.ones((n, 4, 4), np.float32),
                                     extrinsics=ext, intrinsics=np.tile(np.eye(3, dtype=np.float32), (n, 1, 1)))
        if export_dir:
            os.makedirs(os.path.join(export_dir, "sub"), exist_ok=True)
            with open(os.path.join(export_dir, "sub", "scene_x.glb"), "wb") as f:
                f.write(b"glTF-stub")
        return pred
