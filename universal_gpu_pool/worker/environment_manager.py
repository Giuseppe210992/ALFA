import os, tempfile
from pathlib import Path


def local_cache(path=os.path.join(tempfile.gettempdir(), "gpu_pool_cache")):
    p = Path(path); p.mkdir(parents=True, exist_ok=True)
    return p
def fetch_weights(store, name, local):
    """Scarica i pesi dalla cache condivisa (models_cache/) a quella locale, una sola volta."""
    return store.fetch_model(name, local)


def clear_cuda_cache():
    try:
        import torch, gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
