"""Fine-tuning sequenziale a staffetta. Ogni step = `steps_per_turn` global step; il checkpoint
completo viene passato allo step successivo (catena di dipendenze).

IMPORTANTE: questo NON è un pipeline QLoRA pronto all'uso su un modello HF.
params['factory'] = 'modulo:funzione' -> (model, optimizer, scheduler|None, get_batch(global_step)->(x,y), loss_fn)
Senza factory usa un modello giocattolo (Linear) utile solo per testare hand-off e checkpoint.

Per un fine-tuning reale (LoRA/QLoRA) occorre fornire una factory che:
  - carichi il modello base (es. PEFT + bitsandbytes)
  - definisca get_batch sul dataset distillato
  - restituisca loss_fn adeguata
Vedi documentazione PEFT / transformers per un esempio di factory.
"""
import importlib, json, random
from adapters.base_adapter import BaseAdapter


def _toy_factory(p):
    import torch
    model = torch.nn.Linear(16, 1)
    opt = torch.optim.SGD(model.parameters(), lr=p.get("lr", 0.05))
    W = torch.arange(16.0).div(16)

    def get_batch(gs):  # cursore deterministico = global_step
        g = torch.Generator().manual_seed(gs)
        x = torch.randn(8, 16, generator=g)
        return x, (x @ W).unsqueeze(1)
    return model, opt, None, get_batch, torch.nn.functional.mse_loss


class TrainingFinetuneAdapter(BaseAdapter):
    name = "training_finetune"

    def plan(self, items, params, limits):
        total, per = params.get("total_steps", 1000), params.get("steps_per_turn", 200)
        n = -(-total // per)
        return [{"id": f"step_{i:03d}", "depends_on": [f"step_{i-1:03d}"] if i > 1 else [],
                 "payload": {"target_step": min(i * per, total)}} for i in range(1, n + 1)]

    def run_step(self, ctx):
        import math, numpy as np, torch
        p = ctx.params
        if p.get("factory"):
            mod, fn = p["factory"].split(":")
            factory = getattr(importlib.import_module(mod), fn)
        else:
            factory = _toy_factory
        model, opt, sched, get_batch, loss_fn = factory(p)
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(dev)
        scaler = torch.amp.GradScaler("cuda", enabled=(dev == "cuda" and p.get("amp", True)))
        gs = 0
        if ctx.step["depends_on"]:
            ck = torch.load(ctx.store.fetch_output(ctx.job_id, ctx.step["depends_on"][-1]) / "ckpt.pt",
                            map_location=dev, weights_only=False)
            model.load_state_dict(ck["model"]); opt.load_state_dict(ck["optimizer"])
            if sched and ck.get("scheduler"): sched.load_state_dict(ck["scheduler"])
            scaler.load_state_dict(ck["scaler"]); gs = ck["global_step"]
            torch.set_rng_state(ck["rng"]["torch"].cpu()); np.random.set_state(ck["rng"]["numpy"])
            random.setstate(ck["rng"]["python"])
        target, accum, loss_val = ctx.step["payload"]["target_step"], p.get("grad_accum", 1), float("nan")
        start = gs
        model.train()
        while gs < target:
            opt.zero_grad(set_to_none=True)
            for _ in range(accum):  # micro-batch: riduce il picco VRAM
                x, y = get_batch(gs)
                with torch.autocast(dev, enabled=(dev == "cuda")):
                    loss = loss_fn(model(x.to(dev)), y.to(dev)) / accum
                scaler.scale(loss).backward()
                loss_val = loss.item() * accum
            scaler.step(opt); scaler.update()
            if sched: sched.step()
            gs += 1
            ctx.progress((gs - start) / max(target - start, 1))
        ok = math.isfinite(loss_val)
        if ok:  # anti-corruzione: non salva checkpoint con loss NaN/Inf
            torch.save({"model": model.state_dict(), "optimizer": opt.state_dict(),
                        "scheduler": sched.state_dict() if sched else None, "scaler": scaler.state_dict(),
                        "global_step": gs, "data_cursor": gs,
                        "rng": {"torch": torch.get_rng_state(), "numpy": np.random.get_state(),
                                "python": random.getstate()}}, ctx.tmp_dir / "ckpt.pt")
        (ctx.tmp_dir / "meta.json").write_text(json.dumps({"loss": loss_val, "finite": ok, "global_step": gs}))

    def validate(self, ctx):
        m = json.loads((ctx.tmp_dir / "meta.json").read_text())
        return m["finite"] and (ctx.tmp_dir / "ckpt.pt").exists()
