"""Adapter di prova: catena sequenziale (come un training a staffetta) che incrementa un contatore."""
import json, time
from adapters.base_adapter import BaseAdapter


class DemoAdapter(BaseAdapter):
    name = "demo"

    def plan(self, items, params, limits):
        n = params.get("n_steps", 6)
        return [{"id": f"step_{i:03d}", "depends_on": [f"step_{i-1:03d}"] if i > 1 else [],
                 "payload": {"seconds": params.get("step_seconds", 2.0)}} for i in range(1, n + 1)]

    def prefetch(self, ctx):
        time.sleep(0.2)  # simula download pesi + init CUDA

    def run_step(self, ctx):
        prev = 0
        if ctx.step["depends_on"]:
            p = ctx.store.fetch_output(ctx.job_id, ctx.step["depends_on"][-1]) / "state.json"
            prev = json.loads(p.read_text())["value"]
        secs, ticks = ctx.step["payload"]["seconds"], 10
        for t in range(ticks):
            time.sleep(secs / ticks)
            ctx.progress((t + 1) / ticks)
        (ctx.tmp_dir / "state.json").write_text(json.dumps({"value": prev + 1, "by": ctx.worker_name}))
