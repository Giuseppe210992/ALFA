"""
UniversalModelAdapter: un solo adapter per far girare "qualsiasi" modello descritto da una ricetta.

Si integra nel pool come gli altri adapter (plan / run_step).
Supporta runtime:
  - remote_api      Qwen3 235B e simili via API gratuite (RemoteAPIBackend)
  - llama_cpp       server locale effimero (utile su nodi con VRAM sufficiente)
  - hf_transformers HuggingFace con gestione OOM automatica
  - command         comando arbitrario (solo job fidati)

Ricetta tipica (params del job):
{
  "runtime": "remote_api",
  "items": [{"id": "1", "prompt": "..."}, ...],
  "options": {"max_tokens": 512, "temperature": 0.3, "workers": 4}
}

Garanzie:
- output atomico per item (JSONL con flush) e ripartenza
- cache risultati API e pesi
- su OOM riduce batch_size e pulisce cache CUDA
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from adapters.base_adapter import BaseAdapter


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _messages(item):
    return item.get("messages") or [{"role": "user", "content": item.get("prompt", item.get("text", ""))}]


class UniversalModelAdapter(BaseAdapter):
    name = "universal_model"
    RUNTIMES = ("llama_cpp", "hf_transformers", "remote_api", "command")

    def plan(self, items, params, limits):
        """Suddivide gli item in batch. Per remote_api usa batch più grandi (rate-limit gestito dal backend)."""
        runtime = params.get("runtime", "remote_api")
        default_bs = 32 if runtime == "remote_api" else 8
        size = int(params.get("options", {}).get("batch_size", params.get("batch_size", default_bs)))
        # items possono essere già lista di dict o stringhe
        norm = []
        for i, it in enumerate(items):
            if isinstance(it, dict):
                it = dict(it)
                it.setdefault("id", str(i))
                norm.append(it)
            else:
                norm.append({"id": str(i), "prompt": str(it)})
        return [
            {"id": f"step_{k+1:03d}", "depends_on": [], "payload": {"items": norm[i:i + size]}}
            for k, i in enumerate(range(0, len(norm), size))
        ]

    def run_step(self, ctx):
        params = dict(ctx.params)
        runtime = params.get("runtime", "remote_api")
        if runtime not in self.RUNTIMES:
            raise ValueError(f"runtime non valido: {runtime!r}, scegli tra {self.RUNTIMES}")
        opts = params.get("options", {})
        items = list(ctx.step["payload"]["items"])
        out_path = ctx.tmp_dir / "results.jsonl"
        stats = {"ok": 0, "failed": 0}

        # cache_dir condivisa se disponibile
        cache_dir = getattr(ctx, "models_cache", None) or Path("models_cache")
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Idempotenza: salta id già presenti in un eventuale results.jsonl parziale
        done_ids = set()
        if out_path.exists():
            for line in out_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("ok") and rec.get("id") is not None:
                        done_ids.add(str(rec["id"]))
                except Exception:
                    pass
        if done_ids:
            items = [it for it in items if str(it.get("id")) not in done_ids]
            stats["ok"] = len(done_ids)

        if items:
            handler = getattr(self, f"_run_{runtime}")
            handler(params, items, opts, out_path, stats, cache_dir, ctx)
        ctx.progress(1.0)

    # ---------- output atomico ----------
    @staticmethod
    def _write(path, rec, stats):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        stats["ok" if rec.get("ok") else "failed"] += 1

    # ---------- pesi ----------
    def resolve_model(self, ref, cache_dir, hf_file=None):
        if not ref:
            raise ValueError("model_ref mancante")
        p = Path(ref)
        if p.exists():
            return str(p)
        if ref.startswith(("http://", "https://")):
            dest = cache_dir / Path(ref.split("?")[0]).name
            if not dest.exists():
                tmp = dest.with_suffix(dest.suffix + ".part")
                with urllib.request.urlopen(ref, timeout=60) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f, length=1 << 20)
                tmp.replace(dest)
            return str(dest)
        repo, _, fname = ref.partition(":")
        fname = fname or hf_file
        if fname:
            from huggingface_hub import hf_hub_download
            return hf_hub_download(repo, fname, cache_dir=str(cache_dir))
        return repo

    # ---------- runtime: remote_api (FREE path principale) ----------
    def _run_remote_api(self, params, todo, opts, out_path, stats, cache_dir, ctx):
        from core.remote_api_backend import RemoteAPIBackend
        be = RemoteAPIBackend(
            cache_dir=str(cache_dir / "api_results"),
            state_file=str(cache_dir / "api_usage.json"),
        )
        results = be.run_batch(
            todo,
            workers=int(opts.get("workers", 4)),
            max_tokens=opts.get("max_tokens", 512),
            temperature=opts.get("temperature", 0.3),
        )
        for r in results:
            rec = {"id": r["id"], "ok": r["ok"]}
            if r["ok"]:
                rec.update({"text": r.get("text"), "provider": r.get("provider"), "model": r.get("model")})
            else:
                rec["error"] = r.get("error")
            self._write(out_path, rec, stats)
            # progress approssimativo
            done = stats["ok"] + stats["failed"]
            if todo:
                ctx.progress(min(0.99, done / len(todo)))

    # ---------- runtime: llama.cpp (server locale effimero) ----------
    def _run_llama_cpp(self, params, todo, opts, out_path, stats, cache_dir, ctx):
        model = self.resolve_model(params.get("model_ref"), cache_dir)
        binary = opts.get("binary", "llama-server")
        if not shutil.which(binary):
            raise RuntimeError(f"{binary} non trovato nel PATH (compila llama.cpp con CUDA o indica options.binary)")
        port = _free_port()
        cmd = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(port),
               "-c", str(opts.get("n_ctx", 4096)), "-ngl", str(opts.get("n_gpu_layers", 99))]
        cmd += [str(x) for x in opts.get("extra_args", [])]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.time() + opts.get("startup_timeout_s", 900)
            while time.time() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("llama-server terminato in avvio (VRAM insufficiente o modello non valido)")
                try:
                    urllib.request.urlopen(base + "/health", timeout=3).read()
                    break
                except Exception:
                    time.sleep(3)
            else:
                raise TimeoutError("llama-server non è diventato pronto")
            for i, item in enumerate(todo):
                self._chat(base + "/v1/chat/completions", None, item, opts, out_path, stats, model_name="local")
                ctx.progress((i + 1) / len(todo))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _chat(self, url, headers, item, opts, out_path, stats, model_name):
        body = {
            "model": model_name,
            "messages": _messages(item),
            "max_tokens": opts.get("max_tokens", 512),
            "temperature": opts.get("temperature", 0.3),
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=opts.get("request_timeout_s", 600)) as r:
                text = json.loads(r.read())["choices"][0]["message"]["content"]
            self._write(out_path, {"id": item["id"], "ok": True, "text": text}, stats)
        except Exception as e:
            self._write(out_path, {"id": item["id"], "ok": False, "error": str(e)[:300]}, stats)

    # ---------- runtime: HuggingFace transformers ----------
    def _run_hf_transformers(self, params, todo, opts, out_path, stats, cache_dir, ctx):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        ref = self.resolve_model(params.get("model_ref"), cache_dir)
        kw = {"device_map": "auto"}
        if opts.get("quant") == "4bit":
            from transformers import BitsAndBytesConfig
            kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16)
        elif opts.get("quant") == "8bit":
            from transformers import BitsAndBytesConfig
            kw["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
        else:
            kw["torch_dtype"] = torch.float16
        tok = AutoTokenizer.from_pretrained(ref, padding_side="left")
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token
        model = AutoModelForCausalLM.from_pretrained(ref, **kw)
        bs = int(opts.get("batch_size", 8))
        i = 0
        try:
            while i < len(todo):
                chunk = todo[i:i + bs]
                try:
                    prompts = [
                        tok.apply_chat_template(_messages(it), tokenize=False, add_generation_prompt=True)
                        if tok.chat_template else _messages(it)[-1]["content"]
                        for it in chunk
                    ]
                    enc = tok(prompts, return_tensors="pt", padding=True).to(model.device)
                    with torch.no_grad():
                        gen = model.generate(
                            **enc,
                            max_new_tokens=opts.get("max_tokens", 512),
                            do_sample=opts.get("temperature", 0.3) > 0,
                            temperature=max(opts.get("temperature", 0.3), 1e-5),
                        )
                    texts = tok.batch_decode(gen[:, enc["input_ids"].shape[1]:], skip_special_tokens=True)
                    for it, t in zip(chunk, texts):
                        self._write(out_path, {"id": it["id"], "ok": True, "text": t}, stats)
                    i += len(chunk)
                except torch.cuda.OutOfMemoryError:
                    torch.cuda.empty_cache()
                    if bs == 1:
                        self._write(out_path, {"id": chunk[0]["id"], "ok": False, "error": "OOM anche con batch 1"}, stats)
                        i += 1
                    else:
                        bs = max(1, bs // 2)
                finally:
                    torch.cuda.empty_cache()
                ctx.progress(min(0.99, i / len(todo)))
        finally:
            del model
            torch.cuda.empty_cache()

    # ---------- runtime: comando custom ----------
    def _run_command(self, params, todo, opts, out_path, stats, cache_dir, ctx):
        cmd = opts.get("cmd")
        if not cmd or not isinstance(cmd, list):
            raise ValueError("options.cmd deve essere una lista, es. ['python','s.py','{items}','{output}']")
        with tempfile.TemporaryDirectory() as td:
            items_f, out_f = Path(td, "items.json"), Path(td, "out.jsonl")
            items_f.write_text(json.dumps(todo, ensure_ascii=False))
            full = [
                c.replace("{items}", str(items_f))
                 .replace("{output}", str(out_f))
                 .replace("{model}", str(params.get("model_ref", "")))
                for c in cmd
            ]
            proc = subprocess.run(full, capture_output=True, text=True, timeout=opts.get("timeout_s", 3600))
            if proc.returncode != 0:
                raise RuntimeError(f"comando fallito ({proc.returncode}): {proc.stderr[-300:]}")
            seen = set()
            if out_f.exists():
                for line in out_f.read_text(encoding="utf-8").splitlines():
                    rec = json.loads(line)
                    seen.add(str(rec["id"]))
                    self._write(out_path, rec, stats)
            for it in todo:
                if str(it["id"]) not in seen:
                    self._write(out_path, {"id": it["id"], "ok": False, "error": "nessun output dal comando"}, stats)
        ctx.progress(1.0)
