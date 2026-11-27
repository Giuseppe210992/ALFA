"""Finto demo.py di LingBot-Map: stessa struttura (argparse + viewer finale), nessun modello."""
import argparse, os, sys, time
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image_folder"); ap.add_argument("--video_path")
    ap.add_argument("--model_path", required=True)
    ap.add_argument("--fps", type=int, default=10); ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--first_k", type=int); ap.add_argument("--mode", default="streaming")
    ap.add_argument("--use_sdpa", action="store_true"); ap.add_argument("--compile", action="store_true")
    ap.add_argument("--offload_to_cpu", action=argparse.BooleanOptionalAction if hasattr(argparse, "BooleanOptionalAction") else "store_true", default=False)
    ap.add_argument("--keyframe_interval", type=int); ap.add_argument("--num_scale_frames", type=int, default=8)
    ap.add_argument("--camera_num_iterations", type=int, default=4)
    ap.add_argument("--window_size", type=int); ap.add_argument("--overlap_keyframes", type=int)
    args = ap.parse_args()
    assert os.path.isfile(args.model_path), "pesi mancanti"
    if args.first_k == 13:
        print("Running streaming inference...", flush=True)
        print("torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", flush=True)
        sys.exit(1)
    if args.video_path:
        n = 12
    else:
        import glob      # come load_images() del demo.py vero: solo *.jpg,*.png,*.JPG, senza sottocartelle
        n = sum(len(glob.glob(os.path.join(args.image_folder, "*" + e))) for e in (".jpg", ".png", ".JPG"))
        if n == 0:
            raise ValueError("At least 1 image is required")
        print("scale frames:", args.num_scale_frames, flush=True)
    print("Building model...", flush=True); print("  Checkpoint loaded.", flush=True)
    print(f"Input: {n} frames", flush=True); print("Running streaming inference (dtype=x)...", flush=True)
    time.sleep(0.2); print("Inference done in 0.2s", flush=True)
    H, W = 8, 10
    ext = np.zeros((n, 3, 4)); ext[:, :3, :3] = np.eye(3); ext[:, 0, 3] = -np.arange(n) * 0.5
    pred = {"extrinsic": ext, "intrinsic": np.tile(np.eye(3), (n, 1, 1)),
            "depth": np.random.rand(n, H, W, 1).astype("float32"), "depth_conf": np.random.rand(n, H, W).astype("float32"),
            "images": np.random.rand(n, 3, H, W).astype("float32"), "args_seen": args.mode}
    try:
        from lingbot_map.vis import PointCloudViewer
        PointCloudViewer(pred_dict=pred, port=8080, vis_threshold=1.5).run()
    except ImportError:
        print("viser not installed. Install with: pip install lingbot-map[vis]")


if __name__ == "__main__":
    main()
