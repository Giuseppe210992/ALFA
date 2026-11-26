import shutil, subprocess


def gpu_info(default_vram=14.0):
    """-> (nome gpu, vram_totale_gb, vram_libera_gb). Prova torch, poi nvidia-smi, poi default."""
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return torch.cuda.get_device_name(0), total / 2**30, free / 2**30
    except Exception:
        pass
    if shutil.which("nvidia-smi"):
        try:
            o = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                                         "--format=csv,noheader,nounits"], text=True).splitlines()[0]
            n, t, f = [x.strip() for x in o.split(",")]
            return n, float(t) / 1024, float(f) / 1024
        except Exception:
            pass
    return "cpu-sim", default_vram, default_vram
