"""Ricette: piani già pronti per progetti noti (nessuna analisi automatica necessaria)."""
from pathlib import Path

LINGBOT_REPO = "https://github.com/Robbyant/lingbot-map"
DA3_REPO = "https://github.com/ByteDance-Seed/Depth-Anything-3"
DA3_RUNNER_NAME = "_gpool_da3_runner.py"
# fotogrammi massimi per modello sulla GPU gratuita di Kaggle (T4, ~15 GB); se la memoria finisce il programma dimezza da solo
DA3_FRAMES = {"large": 80, "base": 120, "small": 160}
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
    def command(extra_opts, fps, max_frames):
        parts = ["python", RUNNER_NAME, "--repo", ".", "--out", "{OUT}", "--glb-percent", str(float(opts.get("glb_percent", 50))),
                 "--", "--model_path", model, "--mode", opts.get("mode", "streaming")]
        parts += ["--image_folder", FRAMES_DIR]
        merged = dict(extra_opts)
        merged.update({k: v for k, v in opts.items() if v is not None})       # le scelte dell'utente vincono
        for k, flag in (("stride", "--stride"), ("first_k", "--first_k"), ("keyframe_interval", "--keyframe_interval"),
                        ("num_scale_frames", "--num_scale_frames"), ("camera_num_iterations", "--camera_num_iterations"),
                        ("window_size", "--window_size"), ("overlap_keyframes", "--overlap_keyframes")):
            if merged.get(k) is not None:
                parts += [flag, str(int(merged[k]))]
        parts += ["--use_sdpa", "--offload_to_cpu"]       # FlashInfer non è installato su Kaggle: attenzione SDPA
        # Prima si preparano foto o video: il demo ufficiale accetta pochi formati, non guarda nelle sottocartelle e si fida
        # dei metadati dei video (che nei video registrati dal browser sono spesso sbagliati). Si passa sempre da una cartella di fotogrammi.
        prep = f"python {PREP_NAME} {{{key}}} {FRAMES_DIR}"
        if key == "video":
            prep += f" --fps {fps} --max-frames {max_frames}"
        else:
            prep += f" --max-frames {max_frames}"
        return f"{prep} && PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True " + " ".join(parts)

    # La GPU gratuita di Kaggle (T4) ha ~15 GB: la memoria cresce con i fotogrammi. Primo tentativo moderato; se la memoria
    # finisce, secondo tentativo più leggero (meno fotogrammi, keyframe più radi) senza che l'utente debba fare nulla.
    fps = int(opts.get("fps", 5))
    first = command({"keyframe_interval": 4}, fps, int(opts.get("max_frames", 150)))
    second = command({"keyframe_interval": 8, "num_scale_frames": 2}, min(fps, 3), 60)
    run = first if opts.get("no_fallback") else f"({first}) || ({second})"
    return {"setup": setup, "run": run, "input_key": key, "files": {RUNNER_NAME: runner, PREP_NAME: prep},
            "title": "LingBot-Map (3D)", "gpu": True, "timeout_s": int(opts.get("timeout_s", 7200))}


def da3_cfg(scene, opts):
    """Config per l'adapter github_repo che esegue Depth Anything 3 su Kaggle (più veloce di LingBot-Map, ottimo per stanze/oggetti)."""
    adapters = Path(__file__).resolve().parent.parent / "adapters"
    runner = (adapters / "da3_runner.py").read_text(encoding="utf-8")
    prep = (adapters / "lingbot_prep_images.py").read_text(encoding="utf-8")
    key = "video" if scene.get("video") else "images"
    model = opts.get("da3_model", "large")
    if model not in DA3_FRAMES:
        raise ValueError("da3_model deve essere uno tra: " + ", ".join(sorted(DA3_FRAMES)))
    frames = int(opts.get("max_frames", DA3_FRAMES[model]))
    # Il pacchetto non si installa (richiede la cronologia git): si usa src/ direttamente. Le librerie si installano una a una:
    # se una non ha una versione per questo Python non si ferma tutto (il programma ha comunque dei segnaposto).
    setup = opts.get("setup")
    if setup is None:
        setup = ["for p in einops omegaconf safetensors huggingface_hub trimesh imageio opencv-python-headless pillow_heif plyfile; "
                 "do pip install -q $p || true; done"]
    prep_cmd = f"python {PREP_NAME} {{{key}}} {FRAMES_DIR}"
    prep_cmd += f" --fps {int(opts.get('fps', 5))} --max-frames {max(frames * 2, 120)}" if key == "video" else ""
    run_cmd = (f"python {DA3_RUNNER_NAME} --repo . --images {FRAMES_DIR} --out {{OUT}} --model {model} --max-frames {frames} "
               f"--conf-percentile {float(opts.get('conf_percentile', 40))}")
    return {"setup": setup, "run": f"{prep_cmd} && {run_cmd}", "input_key": key,
            "files": {DA3_RUNNER_NAME: runner, PREP_NAME: prep},
            "title": "Depth Anything 3 (3D)", "gpu": True, "timeout_s": int(opts.get("timeout_s", 3600))}
