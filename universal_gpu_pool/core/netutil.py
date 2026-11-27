"""Rete robusta, solo libreria standard: certificati di riserva (Windows 7!), niente proxy
verso localhost, nuovi tentativi, download a blocchi con limite di dimensione.

Windows 7 non riceve più aggiornamenti: i suoi certificati radice possono mancare e causare
«CERTIFICATE_VERIFY_FAILED» verso GitHub/Kaggle. In quel caso si riprova con `cacert.pem`
(raccolta Mozilla, licenza MPL-2.0, dal pacchetto `certifi`) senza MAI disattivare la verifica.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CA_BUNDLE = Path(__file__).with_name("cacert.pem")
UA = "gpu-pool/1.1 (+https://github.com/giuseppe210992/alfa)"
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ctx_default = None
_ctx_bundle = None


def lan_ip():
    """Indirizzo di questo computer nella rete locale (quello da digitare sul telefono), o None."""
    import socket
    cands = []
    try:                                    # nessun pacchetto viene inviato: serve solo a scegliere la scheda di rete in uso
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        cands.append(s.getsockname()[0]); s.close()
    except OSError:
        pass
    try:
        cands += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    for ip in cands:
        parts = ip.split(".")
        if len(parts) == 4 and not ip.startswith(("127.", "169.254.", "0.")):
            return ip
    return None


class NetError(RuntimeError):
    """Errore di rete/HTTP con messaggio leggibile. `.code` è lo status HTTP (0 = rete assente)."""
    def __init__(self, msg, code=0, body=""):
        super().__init__(msg)
        self.code, self.body = code, body


def _is_loopback(url):
    return (urlparse(url).hostname or "").lower() in ("127.0.0.1", "localhost", "::1")


def _context(use_bundle):
    global _ctx_default, _ctx_bundle
    if use_bundle:
        if _ctx_bundle is None:
            _ctx_bundle = ssl.create_default_context(cafile=str(CA_BUNDLE))
        return _ctx_bundle
    if _ctx_default is None:
        _ctx_default = ssl.create_default_context()
    return _ctx_default


def _open(req, timeout):
    url = req.full_url
    if _is_loopback(url):
        return _DIRECT.open(req, timeout=timeout)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_context(False))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, ssl.SSLError) and CA_BUNDLE.exists() and "CERTIFICATE" in str(reason).upper():
            return urllib.request.urlopen(req, timeout=timeout, context=_context(True))
        raise


def _explain(e):
    if isinstance(e, urllib.error.HTTPError):
        return None
    reason = getattr(e, "reason", e)
    txt = str(reason)
    if "CERTIFICATE" in txt.upper():
        return "certificato del sito non riconosciuto dal computer (data/ora del PC corretta? Windows non aggiornato?)"
    if "getaddrinfo" in txt or "Name or service" in txt or "nodename" in txt:
        return "nessuna connessione a internet (o sito non raggiungibile)"
    if "timed out" in txt:
        return "il sito non risponde (timeout)"
    return txt


def request(method, url, headers=None, data=None, json_body=None, timeout=60, retries=2, max_bytes=50_000_000):
    """Ritorna (status, headers, body_bytes). Solleva NetError per errori HTTP (>=400) e di rete."""
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with _open(req, timeout) as r:
                body = r.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise NetError(f"risposta troppo grande da {url}")
                return r.status, dict(r.headers), body
        except urllib.error.HTTPError as e:
            body = e.read()[:5000].decode("utf-8", "replace")
            if e.code >= 500 and attempt < retries:
                time.sleep(1.5 * (attempt + 1)); last = e; continue
            raise NetError(f"HTTP {e.code}", code=e.code, body=body) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1)); continue
            raise NetError(_explain(e) or str(e)) from None
    raise NetError(str(last))


def get_json(url, headers=None, **kw):
    code, _, body = request("GET", url, headers=headers, **kw)
    return json.loads(body.decode("utf-8"))


def post_json(url, payload, headers=None, **kw):
    code, _, body = request("POST", url, headers=headers, json_body=payload, **kw)
    return json.loads(body.decode("utf-8") or "{}")


class _ProgressFile:
    """File in sola lettura che segnala i byte letti (per mostrare l'avanzamento di un caricamento)."""
    def __init__(self, path, cb):
        self._f = open(path, "rb")
        self._cb, self._n = cb, 0

    def read(self, n=-1):
        b = self._f.read(n)
        self._n += len(b)
        if self._cb:
            self._cb(self._n)
        return b

    def close(self):
        self._f.close()


def put_file(url, path, progress=None, timeout=600, headers=None):
    """PUT di un intero file (corpo in streaming, senza caricarlo in memoria). Ritorna lo status HTTP."""
    size = os.path.getsize(path)
    pf = _ProgressFile(path, progress)
    hdrs = {"User-Agent": UA, "Content-Length": str(size)}
    hdrs.update(headers or {})
    try:
        req = urllib.request.Request(url, data=pf, headers=hdrs, method="PUT")
        with _open(req, timeout) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        raise NetError(f"HTTP {e.code}", code=e.code, body=e.read()[:500].decode("utf-8", "replace")) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise NetError(_explain(e) or str(e)) from None
    finally:
        pf.close()


def download(url, dest, headers=None, max_bytes=5_000_000_000, timeout=120, retries=2):
    """Scarica su file (a blocchi, scrittura atomica). Ritorna i byte scaricati."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    for attempt in range(retries + 1):
        n = 0
        try:
            with _open(urllib.request.Request(url, headers=hdrs), timeout) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > max_bytes:
                        raise NetError(f"file oltre il limite di {max_bytes / 1e9:.1f} GB: {url}")
                    f.write(chunk)
            os.replace(tmp, dest)
            return n
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < retries:
                time.sleep(2 * (attempt + 1)); continue
            raise NetError(f"HTTP {e.code} scaricando {url}", code=e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1)); continue
            raise NetError(_explain(e) or str(e)) from None
        finally:
            if tmp.exists() and not dest.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
