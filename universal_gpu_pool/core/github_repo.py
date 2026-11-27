"""Legge un repository GitHub pubblico (senza clonarlo) per capire cosa contiene.

Usa 2 chiamate all'API GitHub (metadati + elenco file) e il sito `raw.githubusercontent.com`
(README e pochi file chiave), così bastano pochi limiti di quota (60 richieste/ora senza account).
Se esiste la variabile GITHUB_TOKEN viene usata per alzare il limite.

Indirizzi cambiabili per i test: GPOOL_GITHUB_API, GPOOL_GITHUB_RAW, GPOOL_GITHUB_CLONE_BASE.
"""
import base64
import os
import re
import time
from urllib.parse import quote

from core import netutil

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
MAX_TREE = 400
README_CHARS = 14000
FILE_CHARS = 6000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".github", "assets", "images", "docs/images", "static"}
BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4", ".avi", ".mov", ".webp", ".ico", ".pdf", ".zip",
              ".tar", ".gz", ".pt", ".pth", ".ckpt", ".bin", ".onnx", ".safetensors", ".npz", ".npy", ".mp3",
              ".wav", ".woff", ".woff2", ".ttf", ".pkl", ".h5")


class RepoError(RuntimeError):
    """Errore comprensibile da mostrare all'utente."""


def api_base():
    return os.environ.get("GPOOL_GITHUB_API", "https://api.github.com").rstrip("/")


def raw_base():
    return os.environ.get("GPOOL_GITHUB_RAW", "https://raw.githubusercontent.com").rstrip("/")


def clone_url(owner, repo):
    base = os.environ.get("GPOOL_GITHUB_CLONE_BASE", "https://github.com").rstrip("/")
    return f"{base}/{owner}/{repo}.git"


def parse_url(text):
    """'https://github.com/o/r', '.../o/r.git', '.../o/r/tree/ramo/cartella', 'git@github.com:o/r.git', 'o/r'
    -> (owner, repo, ref|None). Solleva RepoError se non è un repository GitHub."""
    t = (text or "").strip().strip("<>\"'")
    if not t:
        raise RepoError("Incolla il link del progetto GitHub (es. https://github.com/utente/progetto).")
    m = re.match(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", t)
    if m:
        owner, repo, ref = m.group(1), m.group(2), None
    else:
        t2 = re.sub(r"^(https?://)?(www\.)?github\.com/", "", t, flags=re.I)
        if t2 == t and not re.match(r"^[\w.-]+/[\w.-]+$", t):
            raise RepoError("Questo non sembra un link di GitHub. Deve iniziare con https://github.com/…")
        parts = [p for p in re.split(r"[/?#]", t2.split("?")[0].split("#")[0]) if p]
        if len(parts) < 2:
            raise RepoError("Il link deve indicare utente e progetto: https://github.com/utente/progetto")
        owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
        ref = None
        if len(parts) >= 4 and parts[2] in ("tree", "blob"):
            ref = parts[3]
    if not NAME_RE.match(owner) or not NAME_RE.match(repo):
        raise RepoError("Nome utente o progetto GitHub non valido.")
    return owner, repo, ref


def _headers():
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def _api(path):
    try:
        return netutil.get_json(api_base() + path, headers=_headers(), timeout=30)
    except netutil.NetError as e:
        if e.code == 404:
            raise RepoError("Progetto non trovato. Controlla il link: deve essere pubblico (i progetti privati non sono supportati).") from None
        if e.code in (403, 429):
            wait = ""
            try:
                import json
                msg = json.loads(e.body).get("message", "")
            except Exception:
                msg = e.body
            if "rate limit" in (msg or "").lower():
                wait = " (limite orario di GitHub raggiunto: riprova tra un'ora)"
            raise RepoError("GitHub ha rifiutato la richiesta" + wait + ".") from None
        raise RepoError(f"Impossibile leggere GitHub: {e}") from None


def _raw(owner, repo, ref, path, limit=FILE_CHARS):
    url = f"{raw_base()}/{owner}/{repo}/{quote(ref, safe='')}/{quote(path)}"
    try:
        code, _, body = netutil.request("GET", url, timeout=30, retries=1, max_bytes=2_000_000)
    except netutil.NetError:
        return None
    return body.decode("utf-8", "replace")[:limit]


def _interesting(path):
    low = path.lower()
    if low.endswith(BINARY_EXT):
        return False
    parts = low.split("/")
    return not any(p in SKIP_DIRS for p in parts[:-1])


def _prune_tree(items):
    """Tiene al massimo MAX_TREE percorsi, privilegiando la radice e i file di testo/codice."""
    files = [i for i in items if i.get("type") == "blob" and _interesting(i["path"])]
    files.sort(key=lambda i: (i["path"].count("/"), i["path"].lower()))
    return [{"path": i["path"], "size": i.get("size", 0)} for i in files[:MAX_TREE]], len(files) > MAX_TREE


ENTRY_RE = re.compile(r"(^|/)(app|demo|infer|inference|main|run|predict|generate|test|sample|cli|webui|"
                      r"gradio\w*|streamlit\w*|reconstruct\w*|process\w*|__main__)[\w-]*\.py$", re.I)


def readme_scripts(readme, paths):
    """Script .py citati nei comandi del README (es. `python blur_video.py ...`) che esistono nel repository."""
    named = re.findall(r"python3?\s+(?:-u\s+)?([\w./-]+\.py)", readme or "")
    have = set(paths)
    out = []
    for n in named:
        n = n.lstrip("./")
        if n in have and n not in out:
            out.append(n)
    return out[:4]


def fetch_repo(owner, repo, ref=None):
    meta = _api(f"/repos/{owner}/{repo}")
    ref = ref or meta.get("default_branch") or "main"
    tree_json = _api(f"/repos/{owner}/{repo}/git/trees/{quote(ref, safe='')}?recursive=1")
    tree, truncated = _prune_tree(tree_json.get("tree", []))
    paths = [t["path"] for t in tree]

    readme_path = next((p for p in paths if "/" not in p and p.lower().startswith("readme")), None)
    readme = _raw(owner, repo, ref, readme_path, README_CHARS) if readme_path else ""

    wanted = [p for p in paths if "/" not in p and re.match(
        r"^(requirements[\w.-]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|environment\.ya?ml|Dockerfile|Makefile)$", p)]
    wanted += readme_scripts(readme, paths)
    wanted += [p for p in paths if p.count("/") <= 1 and ENTRY_RE.search(p) and p.endswith(".py") and p not in wanted][:6]
    files = {}
    for p in wanted[:12]:
        txt = _raw(owner, repo, ref, p)
        if txt is not None:
            files[p] = txt
    return {
        "owner": owner, "repo": repo, "ref": ref, "url": f"https://github.com/{owner}/{repo}",
        "description": meta.get("description") or "", "stars": meta.get("stargazers_count", 0),
        "license": ((meta.get("license") or {}).get("spdx_id") or ""), "size_kb": meta.get("size", 0),
        "language": meta.get("language") or "", "archived": bool(meta.get("archived")),
        "tree": paths, "tree_truncated": truncated, "readme": readme or "", "files": files,
        "fetched": time.time(),
    }
