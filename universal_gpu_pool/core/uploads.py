"""File trascinati dall'utente nella webapp: salvati sul disco di questo computer, a blocchi (anche GB),
e poi caricati da qui verso Kaggle (dataset privato). Gli identificativi sono casuali e non indovinabili."""
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(tempfile.gettempdir()) / "gpool_uploads"
ID_RE = re.compile(r"^[a-f0-9]{16}$")


class UploadError(RuntimeError):
    pass


def configure(path):
    global ROOT
    ROOT = Path(path)
    ROOT.mkdir(parents=True, exist_ok=True)
    cleanup()


def safe_name(name):
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^\w.\- ()]", "_", base, flags=re.U).strip(" .")
    return (base or "file")[:120]


def save_stream(rfile, length, name, max_bytes):
    """Legge `length` byte da rfile e li scrive su disco. Ritorna {id, name, size}."""
    if length <= 0:
        raise UploadError("file vuoto")
    if length > max_bytes:
        raise UploadError(f"file troppo grande ({length / 1e9:.1f} GB, massimo {max_bytes / 1e9:.1f} GB)")
    uid = secrets.token_hex(8)
    fname = safe_name(name)
    d = ROOT / uid
    d.mkdir(parents=True, exist_ok=True)
    dest = d / fname
    remaining = length
    try:
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk = rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            raise UploadError("trasferimento interrotto: riprova")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return {"id": uid, "name": fname, "size": length}


def path_for(upload_id):
    """Percorso del file caricato, o None."""
    if not ID_RE.match(str(upload_id or "")):
        return None
    d = ROOT / upload_id
    try:
        files = [p for p in d.iterdir() if p.is_file()]
    except OSError:
        return None
    return files[0] if files else None


def cleanup(max_age_days=7):
    cutoff = time.time() - max_age_days * 86400
    try:
        for d in ROOT.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def parse_ref(value):
    """'upload:<id>' -> id (o None se non è un riferimento a un file caricato)."""
    v = str(value or "")
    return v[7:] if v.startswith("upload:") else None
