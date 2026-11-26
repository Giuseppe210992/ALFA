"""Lettura configurazione: YAML se disponibile, altrimenti parser minimale "chiave: valore".

Se accanto al file esiste `local_config.yaml` (escluso da git) i suoi valori hanno la
precedenza: lì la webapp salva le impostazioni personali (es. il token generato).
"""
import json
from pathlib import Path


def _parse_flat(text):
    d = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        # commento in coda solo se preceduto da spazio (non rompe URL con '#')
        for i, ch in enumerate(s):
            if ch == "#" and i > 0 and s[i - 1] in " \t":
                s = s[:i].rstrip()
                break
        k, v = s.split(":", 1)
        v = v.strip()
        try:
            v = json.loads(v)
        except Exception:
            if v.lower() in ("true", "yes", "on"):
                v = True
            elif v.lower() in ("false", "no", "off"):
                v = False
        d[k.strip()] = v
    return d


def _read(path):
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        return _parse_flat(text)


def load_config(path, local=True):
    path = Path(path)
    cfg = _read(path) if path.exists() else {}
    if local:
        lp = path.with_name("local_config.yaml")
        if lp.exists():
            try:
                cfg.update(_read(lp) or {})
            except Exception:
                pass
    return cfg


def save_local(path, updates):
    """Aggiorna chiavi semplici in local_config.yaml (formato piatto, leggibile da entrambi i parser)."""
    lp = Path(path).with_name("local_config.yaml")
    cur = {}
    if lp.exists():
        try:
            cur = _read(lp) or {}
        except Exception:
            cur = {}
    cur.update(updates)
    body = "# Impostazioni locali (generate da run_webapp.py). Non committare.\n"
    body += "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in cur.items())
    tmp = lp.with_name(lp.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(lp)
    try:
        lp.chmod(0o600)
    except OSError:
        pass
