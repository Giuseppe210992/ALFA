"""«torch» finto minimo, solo per provare da3_runner senza installare torch."""
import types

cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
