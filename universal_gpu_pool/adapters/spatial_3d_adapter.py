from adapters.chunked_command_adapter import ChunkedCommandAdapter


class Spatial3DAdapter(ChunkedCommandAdapter):
    """Ricostruzione 3D: blocchi da ~25-30 keyframe con 4-6 di overlap."""
    name = "spatial_3d"
    default_chunk, default_overlap = 28, 5
