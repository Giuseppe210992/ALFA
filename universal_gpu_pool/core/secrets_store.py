"""Chiavi API locali: config/secrets.env (KEY=valore, permessi 600, escluso da git).

La webapp scrive qui le chiavi inserite dall'utente e le carica in os.environ, così il
worker locale (stesso processo) le usa subito, senza riavvii né `export` nel terminale.
"""
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = ROOT / "config" / "secrets.env"
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_LOCK = threading.Lock()


def valid_name(name):
    return bool(_NAME_RE.match(name or ""))


def read_secrets(path=None):
    path = Path(path or SECRETS_FILE)
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if valid_name(k):
            out[k] = v
    return out


def load_into_env(path=None):
    """Carica le chiavi salvate nell'ambiente del processo. Ritorna i nomi caricati."""
    data = read_secrets(path)
    for k, v in data.items():
        if v:
            os.environ[k] = v
    return sorted(k for k, v in data.items() if v)


def set_secret(name, value, path=None):
    """Salva (o rimuove se value è vuoto) una chiave e aggiorna os.environ."""
    if not valid_name(name):
        raise ValueError(f"nome variabile non valido: {name!r}")
    value = (value or "").strip()
    if any(c in value for c in "\r\n"):
        raise ValueError("la chiave non può contenere a capo")
    path = Path(path or SECRETS_FILE)
    with _LOCK:
        data = read_secrets(path)
        if value:
            data[name] = value
            os.environ[name] = value
        else:
            data.pop(name, None)
            os.environ.pop(name, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        body = "# Chiavi API locali (generato dalla webapp). NON condividere, NON committare.\n"
        body += "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
        # crea il file già con permessi ristretti
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)


def hint(value):
    """Mostra solo le ultime 4 cifre di una chiave."""
    value = value or ""
    return ("…" + value[-4:]) if len(value) >= 8 else ("impostata" if value else "")
