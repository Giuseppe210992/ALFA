"""Launcher: decide QUANTI nodi avviare (regole fisse, deterministiche) e li avvia tramite un backend.
Il planner AI può solo suggerire; qui i limiti (max_nodes, budget GPU, cooldown) sono codice."""
import time

WEEK = 7 * 86400
DONE = ("complete", "error", "cancelled")


class Launcher:
    def __init__(self, cfg, client, backend, store, log=print):
        self.cfg, self.client, self.backend, self.store, self.log = cfg, client, backend, store, log
        self.slots = cfg.get("launcher_slots", [])
        self.max_nodes = cfg.get("max_nodes", 2)
        self.budget_h = cfg.get("gpu_hours_budget_week", 20)
        self.cooldown = cfg.get("launch_cooldown_s", 60)
        self.startup_timeout = cfg.get("startup_timeout_s", 900)
        self.state = store.get_json("launcher_state.json", {}) or {}
        self.state.setdefault("launches", [])
        self.last_launch = 0.0

    def used_hours(self, now):
        tot = 0.0
        for l in self.state["launches"]:
            a, b = max(l["start"], now - WEEK), min(l.get("end") or now, now)
            tot += max(b - a, 0)
        return tot / 3600

    def tick(self):
        now = time.time()
        st = self.client.call("GET", "/status")
        online = {w["name"] for w in st["workers"] if w["status"] == "online"}
        for l in self.state["launches"]:                       # aggiorna lo stato dei lanci
            if not l.get("end") and self.backend.status(l["slot"]) in DONE:
                l["end"] = now
        self.state["launches"] = [l for l in self.state["launches"] if (l.get("end") or now) > now - 14 * 86400]
        active = [l for l in self.state["launches"] if not l.get("end")]
        starting = [l for l in active if l["slot"] not in online and now - l["start"] < self.startup_timeout]
        have = len(online) + len(starting)
        need = min(st["demand"]["nodes_needed"], self.max_nodes)
        used = self.used_hours(now)
        if need > have and now - self.last_launch >= self.cooldown:
            busy = {l["slot"] for l in active}
            free = [s for s in self.slots if s not in busy]
            if not free:
                self.log("nessuno slot libero")
            elif used >= self.budget_h:
                self.log(f"budget GPU settimanale esaurito ({used:.1f}/{self.budget_h} h): non avvio nodi")
            else:
                slot = free[0]
                try:
                    self.backend.push(slot)
                    self.state["launches"].append({"slot": slot, "start": now, "end": None})
                    self.last_launch = now
                    self.log(f"avviato {slot} (nodi attivi {have}, richiesti {need}, usate {used:.1f} h)")
                except Exception as e:
                    self.log(f"avvio di {slot} fallito: {e}")
        self.state.update(used_hours_week=round(used, 2), budget_h=self.budget_h, max_nodes=self.max_nodes,
                          active=[l["slot"] for l in self.state["launches"] if not l.get("end")], updated=now)
        self.store.put_json("launcher_state.json", self.state)
        return {"need": need, "have": have, "used_h": used}

    def run(self, interval=60):
        while True:
            try:
                self.tick()
            except Exception as e:
                self.log(f"tick fallito: {e}")
            time.sleep(interval)
