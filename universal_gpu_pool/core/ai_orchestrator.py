"""Planner: decide COME tagliare il lavoro. Euristico e deterministico, con punto di
aggancio per un LLM consulente (il suo output sarebbe comunque limitato da lo/hi)."""


class Planner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.advice = []          # raccomandazioni accumulate dalla telemetria
        self.chunk_scale = 1.0

    def chunk_size(self, min_vram_gb, weights_gb, gb_per_item, lo=2, hi=64, headroom=1.3):
        free = max(min_vram_gb - weights_gb, 0.5)
        n = int(free / (gb_per_item * headroom))
        return max(lo, min(hi, int(n * self.chunk_scale)))

    def note_pressure(self, worker_id, vram_used, vram_total):
        """Se un nodo supera il 92% di VRAM, riduce i chunk dei job futuri."""
        if vram_total and vram_used / vram_total > 0.92 and self.chunk_scale > 0.3:
            self.chunk_scale *= 0.8
            self.advice.append(f"{worker_id}: VRAM {vram_used:.1f}/{vram_total:.1f} GB -> chunk x{self.chunk_scale:.2f}")

    def llm_advice(self, telemetry):  # punto di estensione (non implementato)
        return None
