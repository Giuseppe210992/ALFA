"""Client minimale per l'API di Kaggle, solo libreria standard (funziona anche con Python 3.8).

Il pacchetto ufficiale `kaggle` richiede Python >= 3.11, quindi qui si parla direttamente con le
stesse chiamate REST dell'SDK ufficiale (kagglesdk): POST JSON su
    https://api.kaggle.com/v1/<servizio>/<metodo>
con `Authorization: Bearer <token>` (token generato da kaggle.com/settings/api) oppure, per le
vecchie chiavi `kaggle.json`, `Authorization: Basic <username:key>`.

L'indirizzo base si può cambiare con GPOOL_KAGGLE_API (usato dai test con un finto Kaggle).
"""
import base64
import os
import re
import time

from core import netutil

DEFAULT_BASE = "https://api.kaggle.com"
KERNELS = "kernels.KernelsApiService"
OAUTH = "security.OAuthService"
TERMINAL = ("COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED")
_STATUS_BY_NUM = {0: "QUEUED", 1: "RUNNING", 2: "COMPLETE", 3: "ERROR", 4: "CANCEL_REQUESTED",
                  5: "CANCEL_ACKNOWLEDGED", 6: "NEW_SCRIPT"}


class KaggleError(RuntimeError):
    pass


def _secs(v):
    """'151.500s' -> 151.5 ; numeri e None tollerati."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).rstrip("s"))
    except ValueError:
        return 0.0


def slugify(title):
    """Kaggle ricava lo slug dal titolo: minuscolo, spazi -> trattini."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")


class KaggleClient:
    def __init__(self, token=None, username=None, key=None, base=None):
        self.token, self.username, self.key = (token or "").strip(), (username or "").strip(), (key or "").strip()
        self.base = (base or os.environ.get("GPOOL_KAGGLE_API") or DEFAULT_BASE).rstrip("/")
        if not (self.token or (self.username and self.key)):
            raise KaggleError("Account Kaggle non collegato. Apri «Account e chiavi» → «GPU» e incolla il token.")
        self._user = self.username or None

    @classmethod
    def from_env(cls):
        return cls(token=os.environ.get("KAGGLE_API_TOKEN"), username=os.environ.get("KAGGLE_USERNAME"),
                   key=os.environ.get("KAGGLE_KEY"))

    @staticmethod
    def configured():
        e = os.environ
        return bool(e.get("KAGGLE_API_TOKEN", "").strip() or (e.get("KAGGLE_USERNAME") and e.get("KAGGLE_KEY")))

    # ---------- HTTP ----------
    def _auth(self):
        if self.token:
            return {"Authorization": "Bearer " + self.token}
        raw = base64.b64encode(f"{self.username}:{self.key}".encode()).decode()
        return {"Authorization": "Basic " + raw}

    def call(self, service, method, payload=None, timeout=60, retries=2):
        url = f"{self.base}/v1/{service}/{method}"
        try:
            return netutil.post_json(url, payload or {}, headers=self._auth(), timeout=timeout, retries=retries)
        except netutil.NetError as e:
            raise KaggleError(self._explain(e)) from None

    @staticmethod
    def _explain(e):
        msg = ""
        try:
            import json
            msg = (json.loads(e.body).get("message") or "").strip()
        except Exception:
            msg = (e.body or "").strip()[:200]
        if e.code == 401:
            return "Kaggle non ti riconosce: token errato, scaduto o revocato. Generane uno nuovo su kaggle.com/settings/api."
        if e.code == 403:
            return ("Kaggle ha negato l'accesso" + (f" ({msg})" if msg else "") +
                    ". Controlla di aver verificato il numero di telefono su kaggle.com → Settings → Phone verification: "
                    "senza, GPU e internet nei notebook sono disattivati.")
        if e.code == 429:
            return "Kaggle: troppe richieste, riprova tra qualche minuto."
        if e.code:
            return f"Kaggle ha risposto {e.code}" + (f": {msg}" if msg else "")
        return f"Impossibile contattare Kaggle: {e}"

    # ---------- account ----------
    def whoami(self):
        """Username dell'account. Con un token si ricava dal servizio OAuth; con la vecchia chiave è noto."""
        if self._user:
            return self._user
        r = self.call(OAUTH, "IntrospectToken", {"token": self.token})
        if not r.get("active") or not r.get("username"):
            raise KaggleError("Token Kaggle non valido o scaduto. Generane uno nuovo su kaggle.com/settings/api.")
        self._user = r["username"]
        return self._user

    def quota(self):
        """Ore GPU usate/totali nella finestra settimanale. None se Kaggle non risponde in modo utile."""
        r = self.call(KERNELS, "GetAcceleratorQuotaStatistics", {})
        g = r.get("gpuQuota") or {}
        total, used, reserved = _secs(g.get("totalTimeAllowed")), _secs(g.get("timeUsed")), _secs(g.get("timeReserved"))
        return {"gpu_total_h": round(total / 3600, 1), "gpu_used_h": round((used + reserved) / 3600, 1),
                "gpu_left_h": round(max(0.0, total - used - reserved) / 3600, 1),
                "refresh": r.get("quotaRefreshTime")}

    # ---------- kernel ----------
    def push_script(self, title, text, gpu=True, internet=True, timeout_s=32400):
        """Crea/aggiorna un notebook privato di tipo script ed avvia l'esecuzione. Ritorna (username, slug)."""
        user = self.whoami()
        slug = slugify(title)
        payload = {"slug": f"{user}/{slug}", "newTitle": title, "text": text, "language": "python",
                   "kernelType": "script", "isPrivate": True, "enableGpu": bool(gpu),
                   "enableInternet": bool(internet), "sessionTimeoutSeconds": int(timeout_s)}
        r = self.call(KERNELS, "SaveKernel", payload, timeout=120)
        if r.get("error"):
            raise KaggleError("Kaggle ha rifiutato lo script: " + str(r["error"]))
        for k in ("invalidDatasetSources", "invalidKernelSources", "invalidModelSources"):
            if r.get(k):
                raise KaggleError(f"Kaggle: sorgenti dati non valide ({k}): {r[k]}")
        return user, slug

    def status(self, user, slug):
        """-> (STATO, messaggio_errore). STATO ∈ QUEUED RUNNING COMPLETE ERROR CANCEL_REQUESTED CANCEL_ACKNOWLEDGED NEW_SCRIPT."""
        r = self.call(KERNELS, "GetKernelSessionStatus", {"userName": user, "kernelSlug": slug})
        st = r.get("status", "QUEUED")
        if isinstance(st, int):
            st = _STATUS_BY_NUM.get(st, "QUEUED")
        st = str(st).split(".")[-1].upper()
        return st, r.get("failureMessage") or ""

    def list_output(self, user, slug):
        """Tutti i file prodotti dal notebook: ([{name, url}], log_testo)."""
        files, log, token = [], "", None
        for _ in range(50):
            req = {"userName": user, "kernelSlug": slug, "pageSize": 200}
            if token:
                req["pageToken"] = token
            r = self.call(KERNELS, "ListKernelSessionOutput", req, timeout=120)
            files += [{"name": f.get("fileName", ""), "url": f.get("url", "")} for f in r.get("files", [])]
            log = r.get("log") or log
            token = r.get("nextPageToken")
            if not token:
                break
        return [f for f in files if f["name"] and f["url"]], log

    def download(self, url, dest, max_bytes=5_000_000_000):
        try:
            return netutil.download(url, dest, max_bytes=max_bytes, timeout=300)
        except netutil.NetError as e:
            raise KaggleError(f"download fallito: {e}") from None

    def delete(self, user, slug):
        try:
            self.call(KERNELS, "DeleteKernel", {"userName": user, "kernelSlug": slug}, retries=0)
        except KaggleError:
            pass        # pulizia facoltativa

    def wait(self, user, slug, timeout_s, on_status=None, poll=10.0, on_tick=None):
        """Attende la fine dell'esecuzione. Ritorna (stato_finale, messaggio).
        on_status(stato) a ogni cambio di stato; on_tick(stato, secondi_trascorsi) a ogni controllo."""
        t0, last = time.time(), None
        errors = 0
        while True:
            try:
                st, msg = self.status(user, slug)
                errors = 0
            except KaggleError:
                errors += 1
                if errors >= 6:
                    raise
                time.sleep(poll); continue
            if on_status and st != last:
                on_status(st)
            last = st
            if on_tick:
                on_tick(st, time.time() - t0)
            if st in TERMINAL:
                return st, msg
            if time.time() - t0 > timeout_s:
                raise KaggleError(f"Kaggle non ha finito entro {int(timeout_s / 60)} minuti (stato: {st})")
            time.sleep(poll)
