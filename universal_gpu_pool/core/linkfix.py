"""Trasforma i link «di condivisione» più comuni in link diretti al file, così chi non è tecnico
può incollare ciò che copia dal browser (Google Drive, Dropbox, GitHub, Hugging Face)."""
import re
from urllib.parse import parse_qs, urlparse, urlunparse, urlencode


def direct_url(url):
    """-> (url_diretto, nota|None). Solleva ValueError con un messaggio chiaro se il link non può funzionare."""
    u = url.strip()
    p = urlparse(u)
    host = (p.hostname or "").lower()
    path = p.path
    q = parse_qs(p.query)

    if host in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"):
        if "/drive/folders/" in path or "/folders/" in path:
            raise ValueError("Questo è il link di una CARTELLA di Google Drive: non posso scaricarla. "
                             "Crea un file ZIP con le foto, caricalo su Drive e incolla il link dello ZIP.")
        m = re.search(r"/file/d/([\w-]{10,})", path) or re.search(r"/d/([\w-]{10,})", path)
        fid = m.group(1) if m else (q.get("id") or [None])[0]
        if not fid:
            raise ValueError("Non riesco a leggere il link di Google Drive: apri il file, «Condividi → Chiunque abbia il link» e copia il link.")
        return (f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t",
                "Link di Google Drive convertito in link diretto (il file deve essere condiviso con «Chiunque abbia il link»).")

    if host.endswith("dropbox.com"):
        q2 = {k: v for k, v in q.items() if k != "dl"}
        q2["dl"] = ["1"]
        return urlunparse(p._replace(query=urlencode(q2, doseq=True))), "Link di Dropbox convertito in link diretto."

    if host == "github.com":
        m = re.match(r"^/([^/]+)/([^/]+)/blob/(.+)$", path)
        if m:
            return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}", "Link GitHub convertito in link diretto."

    if host in ("huggingface.co", "www.huggingface.co") and "/blob/" in path:
        return urlunparse(p._replace(path=path.replace("/blob/", "/resolve/", 1), query="download=true")), \
            "Link Hugging Face convertito in link diretto."

    if host in ("1drv.ms", "onedrive.live.com"):
        raise ValueError("I link di OneDrive non funzionano per il download automatico: usa Google Drive o Dropbox.")
    return u, None
