"""
OnDemandBackend: accende un nodo GPU ad alta VRAM a noleggio (RunPod) solo quando il pool
ha lavoro "heavy" in coda, e lo spegne a coda vuota o al superamento dei tetti di spesa.

Sicurezza economica (tutte attive di default):
- enabled: se False non lancia mai (default consigliato per uso gratis)
- max_nodes, max_hourly_usd, max_total_usd
- max_daily_usd: tetto giornaliero oltre al totale di sessione
- budget_reserve_usd: non lancia se il residuo scende sotto questa riserva
- idle_timeout_s, max_lifetime_s
- stato su disco: dopo un riavvio i pod vengono ritrovati e spenti, non dimenticati
- dry_run: simula senza chiamare l'API

Solo libreria standard. Verifica endpoint sulla documentazione RunPod (cambiano).
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://rest.runpod.io/v1"


def _utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class OnDemandBackend:
    def __init__(self,
                 gpu_type_ids=("NVIDIA H100 80GB HBM3",),
                 gpu_count=4,
                 image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
                 start_cmd="python run_worker.py",
                 pass_env=("S3_ENDPOINT", "S3_BUCKET", "S3_KEY", "S3_SECRET"),
                 volume_gb=400, container_disk_gb=50,
                 max_nodes=1, max_hourly_usd=12.0, max_total_usd=20.0,
                 max_daily_usd=10.0, budget_reserve_usd=1.0,
                 idle_timeout_s=300, max_lifetime_s=4 * 3600,
                 api_key_env="RUNPOD_API_KEY",
                 state_file="models_cache/on_demand_state.json",
                 dry_run=False,
                 enabled=False):
        self.gpu_type_ids, self.gpu_count = list(gpu_type_ids), gpu_count
        self.image, self.start_cmd, self.pass_env = image, start_cmd, list(pass_env)
        self.volume_gb, self.container_disk_gb = volume_gb, container_disk_gb
        self.max_nodes, self.max_hourly_usd = max_nodes, max_hourly_usd
        self.max_total_usd = max_total_usd
        self.max_daily_usd = max_daily_usd
        self.budget_reserve_usd = budget_reserve_usd
        self.idle_timeout_s, self.max_lifetime_s = idle_timeout_s, max_lifetime_s
        self.api_key_env = api_key_env
        self.state_file = Path(state_file)
        self.dry_run = dry_run
        self.enabled = enabled
        self.state = {
            "pods": {},
            "spent_usd": 0.0,
            "spent_today_usd": 0.0,
            "day": _utc_day(),
            "last_work": time.time(),
            "last_tick": time.time(),
        }
        self._load()

    def _load(self):
        try:
            data = json.loads(self.state_file.read_text())
            self.state.update(data)
            # normalizza campi nuovi
            self.state.setdefault("spent_today_usd", 0.0)
            self.state.setdefault("day", _utc_day())
            if self.state.get("day") != _utc_day():
                self.state["day"] = _utc_day()
                self.state["spent_today_usd"] = 0.0
        except Exception:
            pass

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.state_file)

    def _req(self, method, path, body=None):
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"Manca la variabile d'ambiente {self.api_key_env}")
        req = urllib.request.Request(
            API + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"RunPod HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}")

    def _roll_day(self):
        today = _utc_day()
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["spent_today_usd"] = 0.0

    def _accrue_cost(self):
        self._roll_day()
        now = time.time()
        dt_h = max(0.0, (now - self.state["last_tick"]) / 3600)
        rate = sum(p.get("cost_per_hr", 0) for p in self.state["pods"].values())
        delta = rate * dt_h
        self.state["spent_usd"] += delta
        self.state["spent_today_usd"] = self.state.get("spent_today_usd", 0.0) + delta
        self.state["last_tick"] = now

    def spent(self):
        return round(self.state["spent_usd"], 2)

    def spent_today(self):
        self._roll_day()
        return round(self.state.get("spent_today_usd", 0.0), 2)

    def _can_afford_launch(self, estimated_hourly=None):
        """True solo se restano margini su totale, giornaliero e riserva."""
        self._accrue_cost()
        remaining_total = self.max_total_usd - self.state["spent_usd"]
        remaining_daily = self.max_daily_usd - self.state.get("spent_today_usd", 0.0)
        if remaining_total <= self.budget_reserve_usd:
            return False, "riserva di budget insufficiente"
        if remaining_daily <= 0:
            return False, "tetto giornaliero raggiunto"
        # stima: almeno 10 minuti di margine all'hourly max
        hourly = estimated_hourly if estimated_hourly is not None else self.max_hourly_usd
        if remaining_total < (hourly * (10 / 60.0)):
            return False, "residuo insufficiente per 10 minuti di utilizzo"
        return True, "ok"

    def _launch(self):
        ok, reason = self._can_afford_launch()
        if not ok:
            raise RuntimeError(f"Lancio rifiutato: {reason}")

        env = {k: os.environ[k] for k in self.pass_env if k in os.environ}
        body = {
            "name": f"pool-heavy-{int(time.time())}",
            "imageName": self.image,
            "gpuTypeIds": self.gpu_type_ids,
            "gpuCount": self.gpu_count,
            "containerDiskInGb": self.container_disk_gb,
            "volumeInGb": self.volume_gb,
            "volumeMountPath": "/workspace",
            "dockerStartCmd": ["bash", "-lc", self.start_cmd],
            "env": env,
            "cloudType": "SECURE",
        }
        if self.dry_run:
            pod = {"id": f"dry-{int(time.time())}", "costPerHr": 0.0}
            print("[dry_run] avvierei:", json.dumps({**body, "env": list(env)}))
        else:
            pod = self._req("POST", "/pods", body)

        cost = float(pod.get("costPerHr") or pod.get("cost_per_hr") or 0)
        if cost > self.max_hourly_usd:
            if not self.dry_run:
                try:
                    self._req("DELETE", f"/pods/{pod['id']}")
                except Exception:
                    pass
            raise RuntimeError(f"Pod rifiutato: {cost}$/h supera il tetto {self.max_hourly_usd}$/h")

        # seconda barriera: anche con costo orario ok, controlla residuo
        ok, reason = self._can_afford_launch(estimated_hourly=cost or self.max_hourly_usd)
        if not ok:
            if not self.dry_run:
                try:
                    self._req("DELETE", f"/pods/{pod['id']}")
                except Exception:
                    pass
            raise RuntimeError(f"Pod rifiutato dopo pricing: {reason}")

        self.state["pods"][pod["id"]] = {"started": time.time(), "cost_per_hr": cost}
        self._save()
        return pod["id"]

    def _terminate(self, pod_id):
        if not self.dry_run:
            try:
                self._req("DELETE", f"/pods/{pod_id}")
            except RuntimeError as e:
                if "404" not in str(e):
                    raise
        self.state["pods"].pop(pod_id, None)
        self._save()

    def scale(self, demand):
        """Da chiamare a ogni ciclo del launcher. demand = {"nodes_needed": int, ...}."""
        self._accrue_cost()
        now, action = time.time(), "none"
        needed = int(demand.get("nodes_needed", 0))
        if needed > 0:
            self.state["last_work"] = now

        over_budget = (
            self.state["spent_usd"] >= self.max_total_usd
            or self.state.get("spent_today_usd", 0.0) >= self.max_daily_usd
        )
        for pid, p in list(self.state["pods"].items()):
            too_old = now - p["started"] > self.max_lifetime_s
            idle = needed == 0 and now - self.state["last_work"] > self.idle_timeout_s
            if over_budget or too_old or idle:
                self._terminate(pid)
                action = "terminate:" + (
                    "budget" if over_budget else "lifetime" if too_old else "idle"
                )

        if (
            self.enabled
            and needed > 0
            and not over_budget
            and len(self.state["pods"]) < self.max_nodes
        ):
            ok, reason = self._can_afford_launch()
            if ok:
                try:
                    pid = self._launch()
                    action = f"launch:{pid}"
                except RuntimeError as e:
                    action = f"launch_blocked:{e}"
            else:
                action = f"launch_blocked:{reason}"
        elif needed > 0 and not self.enabled:
            action = "disabled"

        self._save()
        return {
            "active": list(self.state["pods"]),
            "spent_usd": self.spent(),
            "spent_today_usd": self.spent_today(),
            "enabled": self.enabled,
            "action": action,
        }

    def shutdown(self):
        self._accrue_cost()
        for pid in list(self.state["pods"]):
            self._terminate(pid)
        self._save()


if __name__ == "__main__":
    be = OnDemandBackend(dry_run=True, enabled=True, state_file="/tmp/od_state.json")
    print(be.scale({"nodes_needed": 1}))
    print(be.scale({"nodes_needed": 0}))
    be.shutdown()
    print(be.state)
