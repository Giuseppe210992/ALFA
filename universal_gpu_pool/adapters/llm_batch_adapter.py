"""Inferenza batch su molti prompt. params['handler'] = 'modulo:funzione' (prompt:str -> str).
Senza handler usa un echo, utile per testare la pipeline."""
import importlib, json
from adapters.base_adapter import BaseAdapter


def _load(spec):
    mod, fn = spec.split(":")
    return getattr(importlib.import_module(mod), fn)


class LLMBatchAdapter(BaseAdapter):
    name = "llm_batch"

    def plan(self, items, params, limits):
        size = params.get("batch_size", 32)
        return [{"id": f"step_{k+1:03d}", "depends_on": [], "payload": {"items": items[i:i + size]}}
                for k, i in enumerate(range(0, len(items), size))]

    def run_step(self, ctx):
        fn = _load(ctx.params["handler"]) if ctx.params.get("handler") else (lambda p: p)
        items = ctx.step["payload"]["items"]
        with open(ctx.tmp_dir / "results.jsonl", "w") as f:
            for i, p in enumerate(items):
                f.write(json.dumps({"prompt": p, "output": fn(p)}) + "\n")
                ctx.progress((i + 1) / len(items))
