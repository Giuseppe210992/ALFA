"""
RemoteAPIBackend: esegue job "heavy" (es. Qwen3 235B) su API gratuite OpenAI-compatibili.

- Rotazione automatica tra provider (fallback in ordine di priorità)
- Rate limit per provider (RPM + richieste/giorno), persistito su disco
- Cooldown/backoff su 429 e 5xx, disattivazione su 401/403/404
- Cache dei risultati su disco (stesso input = zero quota consumata)
- Solo libreria standard, nessuna dipendenza

Le chiavi si leggono da variabili d'ambiente (mai nel codice).
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
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Provider:
    name: str
    base_url: str          # senza /chat/completions
    model: str
    key_env: str           # nome della variabile d'ambiente con la API key
    rpm: int = 20          # richieste al minuto consentite
    rpd: int = 200         # richieste al giorno consentite
    extra_headers: dict = field(default_factory=dict)


# Ordine = priorità. Controlla SEMPRE model id e limiti sul sito del provider.
# Valori prudenziali (26/11/2026): meglio sottostimare le quote free che rischiare addebiti.
# - OpenRouter free: tipicamente ~20 RPM e poche decine di req/giorno (non centinaia).
# - Scaleway: spesso credito una tantum, non quota giornaliera rinnovabile  rpd basso di sicurezza.
# - Cerebras: free tier variabile; se il modello esce dal free, arriverà 401/404 e il provider si disabilita.
DEFAULT_PROVIDERS = [
    Provider("cerebras", "https://api.cerebras.ai/v1",
             "qwen-3-235b-a22b-instruct-2507", "CEREBRAS_API_KEY", rpm=30, rpd=1000),
    Provider("openrouter", "https://openrouter.ai/api/v1",
             "qwen/qwen3-235b-a22b-2507:free", "OPENROUTER_API_KEY", rpm=20, rpd=50),
    Provider("scaleway", "https://api.scaleway.ai/v1",
             "qwen3-235b-a22b-instruct-2507", "SCALEWAY_API_KEY", rpm=20, rpd=100),
]


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _secs_to_midnight_utc() -> float:
    now = datetime.now(timezone.utc)
    tomorrow = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp() + 86400)
    return max(60.0, tomorrow - now.timestamp())


class _ProviderState:
    def __init__(self):
        self.calls = deque()        # timestamp delle chiamate dell'ultimo minuto
        self.day = _utc_day()
        self.day_count = 0
        self.cooldown_until = 0.0
        self.streak = 0             # fallimenti consecutivi
        self.disabled = False


class RemoteAPIBackend:
    def __init__(self, providers=None, cache_dir="models_cache/api_results",
                 state_file="models_cache/api_usage.json",
                 total_timeout=600, request_timeout=120):
        self.providers = providers or DEFAULT_PROVIDERS
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(state_file)
        self.total_timeout = total_timeout
        self.request_timeout = request_timeout
        self._lock = threading.Lock()
        self._state = {p.name: _ProviderState() for p in self.providers}
        self._load_usage()

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
            self.state_file.write_text(json.dumps(data))
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

    def _penalize(self, p: Provider, seconds: float, disable=False):
        with self._lock:
            st = self._state[p.name]
            st.streak += 1
            st.cooldown_until = time.time() + seconds
            if disable:
                st.disabled = True

    def _success(self, p: Provider):
        with self._lock:
            self._state[p.name].streak = 0

    # ---------- chiamata HTTP ----------
    def _call(self, p: Provider, messages, params):
        key = os.environ.get(p.key_env)
        if not key:
            self._penalize(p, 0, disable=True)
            raise RuntimeError(f"{p.name}: manca {p.key_env}")
        body = {"model": p.model, "messages": messages, **params}
        req = urllib.request.Request(
            p.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}", **p.extra_headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="ignore")[:300]
            st = self._state[p.name]
            if e.code == 429:
                retry = e.headers.get("Retry-After")
                if "day" in text.lower() or "daily" in text.lower():
                    wait = _secs_to_midnight_utc()
                else:
                    wait = float(retry) if retry and retry.isdigit() else 60 * (st.streak + 1)
                self._penalize(p, min(wait, 86400))
            elif e.code in (401, 403, 404):
                self._penalize(p, 0, disable=True)
            elif e.code == 402:
                self._penalize(p, _secs_to_midnight_utc())
            else:
                self._penalize(p, min(30 * (st.streak + 1), 600))
            raise RuntimeError(f"{p.name}: HTTP {e.code} {text}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._penalize(p, min(30 * (self._state[p.name].streak + 1), 600))
            raise RuntimeError(f"{p.name}: rete {e}")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            self._penalize(p, 30)
            raise RuntimeError(f"{p.name}: risposta non valida")
        self._success(p)
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()

    # ---------- cache ----------
    def _cache_path(self, messages, params):
        raw = json.dumps({"m": messages, "p": params}, sort_keys=True)
        return self.cache_dir / (hashlib.sha256(raw.encode()).hexdigest() + ".json")

    # ---------- API pubblica ----------
    def generate(self, messages, **params):
        """Una richiesta. Prova i provider in ordine, aspetta se tutti sono in attesa."""
        params.setdefault("temperature", 0.3)
        params.setdefault("max_tokens", 1024)
        cpath = self._cache_path(messages, params)
        if cpath.exists():
            return {**json.loads(cpath.read_text()), "cached": True}

        deadline, last_err = time.time() + self.total_timeout, None
        while time.time() < deadline:
            waits = []
            for p in self.providers:
                w = self._try_acquire(p)
                if w > 0:
                    waits.append(w)
                    continue
                try:
                    text = self._call(p, messages, params)
                    out = {"text": text, "provider": p.name, "model": p.model}
                    tmp = cpath.with_suffix(".tmp")      # scrittura atomica
                    tmp.write_text(json.dumps(out))
                    tmp.replace(cpath)
                    return {**out, "cached": False}
                except RuntimeError as e:
                    last_err = e
            finite = [w for w in waits if w != float("inf")]
            if not finite and all(s.disabled for s in self._state.values()):
                raise RuntimeError(f"Nessun provider disponibile. Ultimo errore: {last_err}")
            time.sleep(min(max(min(finite, default=5), 1), 30))
        raise TimeoutError(f"Timeout: nessun provider ha risposto. Ultimo errore: {last_err}")

    def run_batch(self, items, workers=4, **params):
        """items: [{"id": ..., "prompt": "..."} oppure {"id": ..., "messages": [...]}]
        Ritorna una lista di {"id", "ok", "text"/"error", "provider"}. Non si ferma al primo errore."""
        def one(item):
            msgs = item.get("messages") or [{"role": "user", "content": item["prompt"]}]
            try:
                r = self.generate(msgs, **{**params, **item.get("params", {})})
                return {"id": item.get("id"), "ok": True, **r}
            except Exception as e:
                return {"id": item.get("id"), "ok": False, "error": str(e)}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(one, items))

    def status(self):
        with self._lock:
            return {p.name: {"used_today": self._state[p.name].day_count, "rpd": p.rpd,
                             "disabled": self._state[p.name].disabled,
                             "cooldown_s": max(0, round(self._state[p.name].cooldown_until -
                                 time.time()))}
                    for p in self.providers}


if __name__ == "__main__":
    be = RemoteAPIBackend()
    res = be.run_batch([{"id": 1, "prompt": "Scrivi 'ok' e basta."}], workers=1, max_tokens=20)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    print(be.status())
