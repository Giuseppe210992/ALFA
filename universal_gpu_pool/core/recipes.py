"""Ricette: piani già pronti per progetti noti (nessuna analisi automatica necessaria)."""
from pathlib import Path

LINGBOT_REPO = "https://github.com/Robbyant/lingbot-map"
RUNNER_NAME = "_gpool_lingbot_runner.py"
PREP_NAME = "_gpool_prep_images.py"
FRAMES_DIR = "/tmp/gpool_frames"
WEIGHTS_CMD = ("python -c \"from huggingface_hub import hf_hub_download as h; "
               "print(h('{repo}', '{file}', local_dir='/tmp/lingbot_weights'))\"")


def lingbot_cfg(scene, opts):
    """Config per l'adapter github_repo che esegue LingBot-Map (v1) su Kaggle con il nostro runner senza interfaccia."""
    adapters = Path(__file__).resolve().parent.parent / "adapters"
    runner = (adapters / "lingbot_map_runner.py").read_text(encoding="utf-8")
    prep = (adapters / "lingbot_prep_images.py").read_text(encoding="utf-8")
    key = "video" if scene.get("video") else "images"
    ckpt = opts.get("checkpoint", "lingbot-map.pt")
    if opts.get("model_path"):
        model = opts["model_path"]
        weights = []
    else:
        model = f"/tmp/lingbot_weights/{ckpt}"
        weights = [WEIGHTS_CMD.format(repo=opts.get("hf_repo", "robbyant/lingbot-map"), file=ckpt)]
    setup = opts.get("setup")
    if setup is None:
        setup = ["pip install -q -e . trimesh matplotlib"] + weights
    parts = ["python", RUNNER_NAME, "--repo", ".", "--out", "{OUT}", "--glb-percent", str(float(opts.get("glb_percent", 50))),
             "--", "--model_path", model, "--mode", opts.get("mode", "streaming")]
    parts += ["--image_folder", FRAMES_DIR]
    for k, flag in (("stride", "--stride"), ("first_k", "--first_k"), ("keyframe_interval", "--keyframe_interval"),
                    ("num_scale_frames", "--num_scale_frames"), ("camera_num_iterations", "--camera_num_iterations"),
                    ("window_size", "--window_size"), ("overlap_keyframes", "--overlap_keyframes")):
        if opts.get(k) is not None:
            parts += [flag, str(int(opts[k]))]
    parts += ["--use_sdpa", "--offload_to_cpu"]       # FlashInfer non è installato su Kaggle: attenzione SDPA
    run = " ".join(parts)
    # Prima si preparano foto o video: il demo ufficiale accetta pochi formati, non guarda nelle sottocartelle e si fida
    # dei metadati dei video (che nei video registrati dal browser sono spesso sbagliati). Si passa sempre da una cartella di fotogrammi.
    extra = f" --fps {int(opts.get('fps', 10))}" if key == "video" else ""
    run = f"python {PREP_NAME} {{{key}}} {FRAMES_DIR}{extra} && {run}"
    return {"setup": setup, "run": run, "input_key": key, "files": {RUNNER_NAME: runner, PREP_NAME: prep},
            "title": "LingBot-Map (3D)", "gpu": True, "timeout_s": int(opts.get("timeout_s", 7200))}
