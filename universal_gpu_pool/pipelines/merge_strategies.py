import json, shutil
from pathlib import Path


def concat_outputs(store, job_id, manifest, dest):
    """Copia gli output di ogni step in dest/<step_id>/ (ordine del manifest)."""
    dest = Path(dest); dest.mkdir(parents=True, exist_ok=True)
    for s in manifest["steps"]:
        shutil.copytree(store.fetch_output(job_id, s["id"]), dest / s["id"], dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".ok"))
    return dest


def concat_drop_overlap(items_per_step):
    """Unisce liste di elementi scartando gli `overlap` iniziali di ogni blocco (tranne il primo).
    NB: l'allineamento di scala/posa per la 3D (Sim3 sui frame di overlap) va implementato
    nel tuo adapter, dipende dal modello usato."""
    out = []
    for chunk, ov in items_per_step:
        out.extend(chunk[ov:])
    return out
