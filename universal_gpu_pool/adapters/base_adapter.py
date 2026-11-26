from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class StepContext:
    store: object
    job_id: str
    step: dict
    params: dict
    tmp_dir: Path
    progress: Callable[[float], None]
    worker_name: str = ""
    models_cache: Path = None


class BaseAdapter:
    """Contratto: aggiungere un modello = scrivere un nuovo file in adapters/."""
    name = "base"

    def plan(self, items, params, limits):
        """-> lista di step [{'id','depends_on','payload'}]."""
        raise NotImplementedError

    def prefetch(self, ctx: StepContext):
        """Precarica pesi/dati dello step mentre il nodo precedente sta finendo."""

    def run_step(self, ctx: StepContext):
        """Scrive i risultati in ctx.tmp_dir. Chiamare ctx.progress(0..1)."""
        raise NotImplementedError

    def validate(self, ctx: StepContext) -> bool:
        return any(ctx.tmp_dir.iterdir())

    def merge(self, store, job_id, manifest, dest):
        """Ricomposizione finale (default: copia ordinata degli output)."""
        from pipelines.merge_strategies import concat_outputs
        return concat_outputs(store, job_id, manifest, dest)
