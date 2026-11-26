"""
RemoteAPIBackend: esegue job "heavy" su API gratuite OpenAI-compatibili.

- Rotazione automatica tra provider (fallback in ordine di priorità)
- Rate limit per provider (RPM + richieste/giorno), persistito su disco
- Cooldown/backoff su 429 e 5xx, disattivazione su 401/403/404
- Cache dei risultati su disco (stesso input = zero quota consumata)
- Provider configurabili in config/providers.json (modificabili dalla webapp)
- Solo libreria standard, nessuna dipendenza

Le chiavi si leggono da variabili d'ambiente (mai nel codice). La webapp le salva
in config/secrets.env (file locale, escluso da git) e le carica nell'ambiente.
Verifica model id e base_url sul sito di ogni provider: le liste free cambiano spesso.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_FILE = ROOT / "config" / "providers.json"


@dataclass
class Provider:
    name: str
    base_url: str          # senza /chat/completions
    model: str
    key_env: str           # nome della variabile d'ambiente con la API key
    rpm: int = 20          # richieste al minuto consentite
    rpd: int = 200         # richieste al giorno consentite
    extra_headers: dict = field(default_factory=dict)
    enabled: bool = True
    label: str = ""
    signup_url: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, d):
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        p = cls(**known)
        p.rpm, p.rpd = max(1, int(p.rpm)), max(1, int(p.rpd))
        return p

    def has_key(self):
        return bool(os.environ.get(self.key_env, "").strip())


# Ordine = priorità. Valori verificati il 26/11/2026 (le liste free cambiano: controlla sul sito).
# - Cerebras: l'API pubblica oggi serve gpt-oss-120b e qwen-3.8-27b (Qwen3-235B non più disponibile).
# - Groq: gpt-oss-120b gratuito con limiti giornalieri.
# - Google AI Studio (Gemini): endpoint OpenAI-compatibile, free tier con limiti bassi al minuto.
# - OpenRouter: "openrouter/free" sceglie in automatico un modello gratuito disponibile.
# - Scaleway: credito iniziale, non quota rinnovabile -> rpd basso di sicurezza.
# - Personalizzato: qualsiasi endpoint OpenAI-compatibile (es. un tuo router FreeLLMAPI, LiteLLM...).
DEFAULT_PROVIDERS = [
    Provider("cerebras", "https://api.cerebras.ai/v1", "gpt-oss-120b", "CEREBRAS_API_KEY",
             rpm=30, rpd=1000, label="Cerebras", signup_url="https://cloud.cerebras.ai/",
             notes="Velocissimo. Modelli free: gpt-oss-120b, qwen-3.8-27b."),
    Provider("groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "GROQ_API_KEY",
             rpm=30, rpd=1000, label="Groq", signup_url="https://console.groq.com/keys",
             notes="Free tier generoso. Altri modelli: qwen/qwen3-32b, llama-3.3-70b-versatile."),
    Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash",
             "GEMINI_API_KEY", rpm=10, rpd=250, label="Google Gemini",
             signup_url="https://aistudio.google.com/apikey",
             notes="Chiave gratuita da Google AI Studio."),
    Provider("openrouter", "https://openrouter.ai/api/v1", "openrouter/free", "OPENROUTER_API_KEY",
             rpm=20, rpd=50, label="OpenRouter", signup_url="https://openrouter.ai/keys",
             extra_headers={"X-Title": "GPU Pool"},
             notes="openrouter/free = sceglie un modello gratuito. Oppure un id con suffisso :free."),
    Provider("scaleway", "https://api.scaleway.ai/v1", "qwen3-235b-a22b-instruct-2507",
             "SCALEWAY_API_KEY", rpm=20, rpd=100, label="Scaleway",
             signup_url="https://console.scaleway.com/generative-api/models",
             notes="Credito iniziale gratuito; unico con Qwen3 235B tra questi."),
    Provider("custom", "", "auto", "CUSTOM_API_KEY", rpm=60, rpd=5000, enabled=True,
             label="Personalizzato (OpenAI-compatibile)",
             notes="Es. il tuo router FreeLLMAPI/LiteLLM: inserisci URL base (…/v1), modello e chiave."),
]


def default_providers():
    return [Provider.from_dict(asdict(p)) for p in DEFAULT_PROVIDERS]


def load_providers(path=None):
    """Legge config/providers.json; se manca o è rotto usa i default.
    I provider di default assenti dal file vengono aggiunti in coda (aggiornamenti futuri)."""
    path = Path(path or PROVIDERS_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw["providers"] if isinstance(raw, dict) else raw
        provs = [Provider.from_dict(d) for d in items if d.get("name")]
    except Exception:
        return default_providers()
    names = {p.name for p in provs}
    provs += [p for p in default_providers() if p.name not in names]
    return provs


def save_providers(providers, path=None):
    path = Path(path or PROVIDERS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"providers": [asdict(p) for p in providers]}, indent=2,
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _secs_to_midnight_utc() -> float:
    now = datetime.now(timezone.utc)
    tomorrow = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp() + 86400)
    return max(60.0, tomorrow - now.timestamp())


# Un 429 "giornaliero" va riconosciuto da frasi esplicite, non da una parola qualsiasi:
# altrimenti un limite al minuto bloccherebbe il provider fino a mezzanotte.
_DAILY_RE = re.compile(r"per[ _-]?day|daily|requests per day|tokens per day|\bRPD\b|\bTPD\b|"
                       r"quota exceeded for .*day|free-models-per-day", re.I)


def _parse_retry_after(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class _ProviderState:
    def __init__(self):
        self.calls = deque()        # timestamp delle chiamate dell'ultimo minuto
        self.day = _utc_day()
        self.day_count = 0
        self.cooldown_until = 0.0
        self.streak = 0             # fallimenti consecutivi
        self.disabled = False
        self.last_error = ""
        self.last_ok = 0.0


class NoProviderError(RuntimeError):
    """Nessun provider utilizzabile (nessuna chiave, tutti disabilitati o quote esaurite)."""


class RemoteAPIBackend:
    def __init__(self, providers=None, cache_dir="models_cache/api_results",
                 state_file="models_cache/api_usage.json",
                 total_timeout=600, request_timeout=120):
        self.providers = providers if providers is not None else load_providers()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(state_file)
        self.total_timeout = total_timeout
        self.request_timeout = request_timeout
        self._lock = threading.Lock()
        self._state = {p.name: _ProviderState() for p in self.providers}
        self._load_usage()

    def active_providers(self):
        """Provider abilitati, con chiave e con URL: solo questi vengono usati."""
        return [p for p in self.providers if p.enabled and p.base_url and p.has_key()]

    # ---------- persistenza uso giornaliero ----------
    def _load_usage(self):
        try:
            data = json.loads(self.state_file.read_text())
        except Exception:
            return
        today = _utc_day()
        for name, st in self._state.items():
            d = data.get(name)
            if d and d.get("day") == today:
                st.day, st.day_count = today, int(d.get("count", 0))

    def _save_usage(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {n: {"day": s.day, "count": s.day_count}
                    for n, s in self._state.items()}
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.state_file)
        except Exception:
            pass

    # ---------- rate limiting ----------
    def _try_acquire(self, p: Provider) -> float:
        """Ritorna 0 se la chiamata può partire, altrimenti i secondi da attendere."""
        with self._lock:
            st, now = self._state[p.name], time.time()
            if st.disabled:
                return float("inf")
            if st.day != _utc_day():
                st.day, st.day_count = _utc_day(), 0
            if st.day_count >= p.rpd:
                return _secs_to_midnight_utc()
            if now < st.cooldown_until:
                return st.cooldown_until - now
            while st.calls and now - st.calls[0] >= 60:
                st.calls.popleft()
            if len(st.calls) >= p.rpm:
                return 60 - (now - st.calls[0]) + 0.05
            st.calls.append(now)
            st.day_count += 1
            self._save_usage()
            return 0.0

    def _penalize(self, p: Provider, seconds: float, disable=False, error=""):
        with self._lock:
            st = self._state[p.name]
            st.streak += 1
            st.cooldown_until = time.time() + seconds
            st.last_error = error[:300]
            if disable:
                st.disabled = True

    def _success(self, p: Provider):
        with self._lock:
            st = self._state[p.name]
            st.streak, st.last_error, st.last_ok = 0, "", time.time()

    # ---------- chiamata HTTP ----------
    def _call(self, p: Provider, messages, params):
        key = os.environ.get(p.key_env, "").strip()
        if not key:
            self._penalize(p, 0, disable=True, error=f"manca {p.key_env}")
            raise RuntimeError(f"{p.name}: manca {p.key_env}")
        body = {"model": p.model, "messages": messages, **params}
        req = urllib.request.Request(
            p.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "gpu-pool/1.0",
                     "Authorization": f"Bearer {key}", **(p.extra_headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="ignore")[:300]
            st = self._state[p.name]
            msg = f"{p.name}: HTTP {e.code} {text}".strip()
            if e.code == 429:
                retry = _parse_retry_after(e.headers.get("Retry-After"))
                if retry is not None:
                    wait = retry + 1
                elif _DAILY_RE.search(text):
                    wait = _secs_to_midnight_utc()
                else:
                    wait = min(20 * (st.streak + 1), 300)
                self._penalize(p, min(wait, 86400), error=msg)
            elif e.code in (401, 403):
                self._penalize(p, 0, disable=True, error=msg + " (chiave non valida?)")
            elif e.code == 404:
                self._penalize(p, 0, disable=True, error=msg + f" (modello '{p.model}' o URL errato?)")
            elif e.code == 402:
                self._penalize(p, _secs_to_midnight_utc(), error=msg + " (credito esaurito)")
            elif e.code == 400:
                self._penalize(p, 30, error=msg)
            else:
                self._penalize(p, min(30 * (st.streak + 1), 600), error=msg)
            raise RuntimeError(msg)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            msg = f"{p.name}: rete/risposta {e}"
            self._penalize(p, min(30 * (self._state[p.name].streak + 1), 600), error=msg)
            raise RuntimeError(msg)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            err = data.get("error") if isinstance(data, dict) else None
            msg = f"{p.name}: risposta non valida {str(err or data)[:200]}"
            self._penalize(p, 30, error=msg)
            raise RuntimeError(msg)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        if not text:
            msg = f"{p.name}: risposta vuota (prova ad aumentare max_tokens)"
            self._penalize(p, 15, error=msg)
            raise RuntimeError(msg)
        self._success(p)
        return text

    # ---------- cache ----------
    def _cache_path(self, messages, params):
        raw = json.dumps({"m": messages, "p": params}, sort_keys=True)
        return self.cache_dir / (hashlib.sha256(raw.encode()).hexdigest() + ".json")

    # ---------- API pubblica ----------
    def generate(self, messages, use_cache=True, **params):
        """Una richiesta. Prova i provider in ordine, aspetta se tutti sono in attesa."""
        params.setdefault("temperature", 0.3)
        params.setdefault("max_tokens", 1024)
        cpath = self._cache_path(messages, params)
        if use_cache and cpath.exists():
            try:
                return {**json.loads(cpath.read_text()), "cached": True}
            except Exception:
                pass

        active = self.active_providers()
        if not active:
            raise NoProviderError(
                "Nessuna chiave API configurata. Apri la webapp > «Account e chiavi» e "
                "inserisci almeno una chiave (es. Groq, Cerebras o Gemini: sono gratuite).")

        deadline, last_err = time.time() + self.total_timeout, None
        while time.time() < deadline:
            waits = []
            for p in active:
                w = self._try_acquire(p)
                if w > 0:
                    waits.append(w)
                    continue
                try:
                    text = self._call(p, messages, params)
                    out = {"text": text, "provider": p.name, "model": p.model}
                    if use_cache:
                        tmp = cpath.with_name(f"{cpath.stem}.{uuid.uuid4().hex[:8]}.tmp")
                        tmp.write_text(json.dumps(out))     # scrittura atomica
                        tmp.replace(cpath)
                    return {**out, "cached": False}
                except RuntimeError as e:
                    last_err = e
            finite = [w for w in self._waits(active) if w != float("inf")]
            if not finite:
                raise NoProviderError(f"Nessun provider disponibile. Ultimo errore: {last_err}")
            remaining = deadline - time.time()
            if min(finite) > remaining:
                raise NoProviderError(
                    f"Tutti i provider sono in pausa (quote/limiti) per almeno {int(min(finite))} s. "
                    f"Ultimo errore: {last_err}")
            time.sleep(min(max(min(finite), 0.5), 30))
        raise TimeoutError(f"Timeout: nessun provider ha risposto. Ultimo errore: {last_err}")

    def _waits(self, providers):
        """Attese correnti senza consumare quota (per decidere se aspettare o arrendersi)."""
        out, now = [], time.time()
        with self._lock:
            for p in providers:
                st = self._state[p.name]
                if st.disabled:
                    out.append(float("inf"))
                elif st.day_count >= p.rpd and st.day == _utc_day():
                    out.append(_secs_to_midnight_utc())
                else:
                    w = max(0.0, st.cooldown_until - now)
                    recent = [t for t in st.calls if now - t < 60]
                    if len(recent) >= p.rpm:
                        w = max(w, 60 - (now - recent[0]))
                    out.append(w)
        return out

    def run_batch(self, items, workers=4, on_result=None, **params):
        """items: [{"id": ..., "prompt": "..."} oppure {"id": ..., "messages": [...]}]
        Ritorna una lista di {"id", "ok", "text"/"error", "provider"} nello stesso ordine.
        Non si ferma al primo errore. on_result(rec) viene chiamato appena un item finisce."""
        def one(item):
            msgs = item.get("messages") or [{"role": "user", "content": item["prompt"]}]
            try:
                r = self.generate(msgs, **{**params, **item.get("params", {})})
                return {"id": item.get("id"), "ok": True, **r}
            except Exception as e:
                return {"id": item.get("id"), "ok": False, "error": str(e)}
        results = [None] * len(items)
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = {ex.submit(one, it): i for i, it in enumerate(items)}
            for f in as_completed(futs):
                rec = f.result()
                results[futs[f]] = rec
                if on_result:
                    on_result(rec)
        return results

    def test_provider(self, name, prompt="Rispondi solo con la parola: OK", max_tokens=64):
        """Prova un singolo provider (senza cache e senza rotazione). Per il pulsante «Prova»."""
        p = next((x for x in self.providers if x.name == name), None)
        if not p:
            return {"ok": False, "error": f"provider sconosciuto: {name}"}
        if not p.base_url:
            return {"ok": False, "error": "URL base mancante"}
        if not p.has_key():
            return {"ok": False, "error": f"chiave mancante ({p.key_env})"}
        t0 = time.time()
        try:
            # temperatura 0 e un po' di margine: i modelli "reasoning" consumano token per pensare
            text = self._call(p, [{"role": "user", "content": prompt}],
                              {"temperature": 0, "max_tokens": max_tokens})
            with self._lock:
                st = self._state[p.name]
                st.day_count += 1
                self._save_usage()
            return {"ok": True, "text": text[:200], "model": p.model,
                    "latency_s": round(time.time() - t0, 2)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:400], "latency_s": round(time.time() - t0, 2)}

    def status(self):
        with self._lock:
            now = time.time()
            return {p.name: {"used_today": self._state[p.name].day_count if self._state[p.name].day == _utc_day() else 0,
                             "rpd": p.rpd, "rpm": p.rpm, "model": p.model,
                             "enabled": p.enabled, "has_key": p.has_key(),
                             "disabled": self._state[p.name].disabled,
                             "last_error": self._state[p.name].last_error,
                             "last_ok": self._state[p.name].last_ok,
                             "cooldown_s": max(0, round(self._state[p.name].cooldown_until - now))}
                    for p in self.providers}


# ---------- backend condiviso (stessi cooldown/quote per tutti gli step del processo) ----------
_SHARED = {"backend": None, "sig": None}
_SHARED_LOCK = threading.Lock()


def _signature():
    try:
        mtime = PROVIDERS_FILE.stat().st_mtime
    except OSError:
        mtime = 0
    keys = tuple(sorted((p.key_env, hashlib.sha256(os.environ.get(p.key_env, "").encode()).hexdigest()[:8])
                        for p in load_providers()))
    return (mtime, keys)


def default_cache_dir():
    """Cartella cache comune del processo (risultati API + contatori di quota)."""
    import tempfile
    return Path(os.environ.get("GPU_POOL_API_CACHE") or Path(tempfile.gettempdir()) / "gpu_pool_api_cache")


def get_shared_backend(cache_dir=None, state_file=None):
    """Ritorna un backend unico per processo, ricreato solo se providers.json o le chiavi cambiano.
    Router (pulsante «Prova», stato quote) e worker locale vedono così gli stessi cooldown."""
    base = default_cache_dir()
    cache_dir = cache_dir or base / "api_results"
    state_file = state_file or base / "api_usage.json"
    with _SHARED_LOCK:
        sig = _signature()
        if _SHARED["backend"] is None or _SHARED["sig"] != sig:
            _SHARED["backend"] = RemoteAPIBackend(cache_dir=cache_dir, state_file=state_file)
            _SHARED["sig"] = sig
        return _SHARED["backend"]


def reset_shared_backend():
    with _SHARED_LOCK:
        _SHARED["backend"], _SHARED["sig"] = None, None


if __name__ == "__main__":
    be = RemoteAPIBackend()
    res = be.run_batch([{"id": 1, "prompt": "Scrivi 'ok' e basta."}], workers=1, max_tokens=64)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    print(json.dumps(be.status(), indent=2))
