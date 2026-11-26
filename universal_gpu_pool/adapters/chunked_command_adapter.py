"""Base per inferenza a macro-blocchi con overlap; il calcolo reale è un comando esterno
(params['command'], con segnaposto {inputs_file} e {output_dir})."""
import json, subprocess
from adapters.base_adapter import BaseAdapter


class ChunkedCommandAdapter(BaseAdapter):
    name = "chunked"
    default_chunk, default_overlap = 28, 5
    gb_per_item, weights_gb = 0.3, 4.0

    def plan(self, items, params, limits):
        ov = params.get("overlap", self.default_overlap)
        size = params.get("chunk_size") or min(
            self.default_chunk + 2,
            limits["planner"].chunk_size(limits["min_vram_gb"], params.get("weights_gb", self.weights_gb),
                                         params.get("gb_per_item", self.gb_per_item)))
        size = max(size, ov + 1)
        steps, i, k = [], 0, 1
        while i < len(items):
            chunk = items[i:i + size]
            steps.append({"id": f"step_{k:03d}", "depends_on": [],
                          "payload": {"items": chunk, "overlap": 0 if i == 0 else ov}})
            if i + size >= len(items):
                break
            i += size - ov
            k += 1
        return steps

    def run_step(self, ctx):
        cmd = ctx.params.get("command")
        if not cmd:
            raise NotImplementedError("Imposta params['command'] con il tuo modello (es. script di ricostruzione 3D)")
        inp = ctx.tmp_dir / "inputs.json"
        inp.write_text(json.dumps(ctx.step["payload"]["items"]))
        out = ctx.tmp_dir / "result"
        out.mkdir()
        subprocess.run(cmd.format(inputs_file=inp, output_dir=out), shell=True, check=True)
        (ctx.tmp_dir / "meta.json").write_text(json.dumps({"overlap": ctx.step["payload"]["overlap"],
                                                           "n": len(ctx.step["payload"]["items"])}))
        ctx.progress(1.0)
