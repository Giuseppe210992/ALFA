"""Adapter «Programma da GitHub»: esegue un repository GitHub su una GPU gratuita di Kaggle.

Il lavoro si orchestra dal tuo computer (nessuna GPU necessaria qui): l'adapter manda a Kaggle uno
script (`core/kaggle_script.py`) che scarica il progetto, lo installa e lo esegue; poi scarica i
file prodotti. Serve solo un token Kaggle (Account e chiavi → GPU). Non servono server pubblici.

params:
    repo_url   https://github.com/utente/progetto           (obbligatorio)
    ref        ramo, tag o commit (facoltativo)
    plan       piano normalizzato da core.repo_planner (setup, run, inputs, outputs, gpu, timeout_min)
    values     valori scelti dall'utente per gli input del piano
    options    gpu (True) · poll_s (10) · delete_after (True)

Output: tutti i file prodotti (outputs/…), run.log, result.json, results.jsonl.
"""
import json
import time
import uuid
from pathlib import Path

from adapters.base_adapter import BaseAdapter
from core import github_repo, repo_planner
from core.kaggle_client import KaggleClient, KaggleError, slugify
from core.repo_planner import normalize_plan, validate_values


class GitHubRepoAdapter(BaseAdapter):
    name = "github_repo"

    # ---------- piano ----------
    def plan(self, items, params, limits):
        if not params.get("repo_url"):
            raise ValueError("manca il link del progetto GitHub (repo_url)")
        try:
            owner, repo, ref = github_repo.parse_url(params["repo_url"])
        except github_repo.RepoError as e:
            raise ValueError(str(e))
        ref = params.get("ref") or ref or ""
        plan = normalize_plan(params.get("plan") or {}, source=(params.get("plan") or {}).get("source", "manuale"))
        if not plan["run"]:
            raise ValueError("manca il comando da eseguire (apri «Dettagli tecnici» e scrivilo)")
        values = validate_values(plan, params.get("values") or {})
        downloads = {i["key"]: values[i["key"]] for i in plan["inputs"] if i["type"] == "url" and values.get(i["key"])}
        plain = {k: v for k, v in values.items() if k not in downloads}
        opts = params.get("options") or {}
        cfg = {"repo_url": f"https://github.com/{owner}/{repo}", "clone_url": github_repo.clone_url(owner, repo),
               "ref": ref, "setup": plan["setup"], "run": plan["run"], "values": plain, "downloads": downloads,
               "outputs": plan["outputs"], "timeout_s": int(plan["timeout_min"]) * 60,
               "setup_timeout_s": 2400, "gpu": bool(opts.get("gpu", plan["gpu"])), "title": plan["title"] or repo}
        return [{"id": "step_001", "depends_on": [], "payload": {"cfg": cfg}}]

    # ---------- esecuzione ----------
    def run_step(self, ctx):
        from core.kaggle_script import build_script
        cfg = dict(ctx.step["payload"]["cfg"])
        opts = dict((ctx.params or {}).get("options") or {})
        out = Path(ctx.tmp_dir)
        t0 = time.time()
        outcome = {"ok": False, "error": "", "files": [], "seconds": 0}
        client = user = slug = None
        try:
            client = KaggleClient.from_env()
            ctx.progress(0.02)
            title = "GPool " + uuid.uuid4().hex[:8]
            user, slug = client.push_script(title, build_script(cfg), gpu=cfg.get("gpu", True), internet=True,
                                            timeout_s=min(int(cfg["timeout_s"]) + cfg["setup_timeout_s"] + 600, 32400))
            print(f"[github_repo] notebook Kaggle: {user}/{slug}", flush=True)
            ctx.progress(0.08)
            budget = int(cfg["timeout_s"]) + cfg["setup_timeout_s"] + 900
            ctx.progress(0.1)

            best = [0.1]

            def tick(st, elapsed):
                p = 0.1
                if st == "RUNNING":
                    p = min(0.9, 0.2 + 0.65 * (1 - 1 / (1 + elapsed / 600.0)))
                elif st in ("COMPLETE", "ERROR"):
                    p = 0.9
                if p > best[0]:             # l'avanzamento non torna mai indietro
                    best[0] = p
                    ctx.progress(p)
            st, msg = client.wait(user, slug, budget, on_tick=tick, poll=float(opts.get("poll_s", 10)))
            ctx.progress(0.92)
            files, klog = client.list_output(user, slug)
            for f in files:
                name = f["name"].replace("\\", "/").lstrip("/")
                if ".." in name.split("/") or not name:
                    continue
                client.download(f["url"], out / name)
            ctx.progress(0.97)
            res = {}
            try:
                res = json.loads((out / "result.json").read_text(encoding="utf-8"))
            except Exception:
                pass
            if res:
                outcome.update(ok=bool(res.get("ok")), error=res.get("error", ""))
            else:
                outcome.update(ok=False, error=(msg or f"Kaggle ha terminato con stato {st} senza risultati"))
                if klog and not (out / "run.log").exists():
                    (out / "run.log").write_text(klog[-20000:], encoding="utf-8")
            if st not in ("COMPLETE",) and outcome["ok"]:
                outcome.update(ok=False, error=msg or f"stato Kaggle: {st}")
        except (KaggleError, OSError) as e:
            outcome.update(ok=False, error=str(e))
        finally:
            if client and user and slug and opts.get("delete_after", True):
                client.delete(user, slug)

        outcome["seconds"] = round(time.time() - t0, 1)
        names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name != "results.jsonl")
        if not outcome["ok"]:
            tail = ""
            try:
                lines = (out / "run.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()
                tail = "\n".join(lines[-25:])
            except Exception:
                pass
            text = f"✗ {outcome['error'] or 'Esecuzione non riuscita'}" + (f"\n\nUltime righe del registro:\n{tail}" if tail else "")
        else:
            produced = [n for n in names if n.startswith("outputs/")]
            text = (f"✓ Completato in {int(outcome['seconds'] // 60)} min {int(outcome['seconds'] % 60)} s. "
                    + (f"{len(produced)} file prodotti: scaricali qui sotto." if produced else
                       "Il programma è finito ma non ha prodotto file: guarda run.log."))
        rec = {"id": cfg.get("title", "programma"), "ok": outcome["ok"], "seconds": outcome["seconds"], "files": names}
        rec["text" if outcome["ok"] else "error"] = text
        with open(out / "results.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.last_metrics = {"ok": 1 if outcome["ok"] else 0, "failed": 0 if outcome["ok"] else 1}
        ctx.progress(1.0)
