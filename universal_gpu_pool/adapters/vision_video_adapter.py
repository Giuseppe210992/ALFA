from adapters.chunked_command_adapter import ChunkedCommandAdapter


class VisionVideoAdapter(ChunkedCommandAdapter):
    """Video/diffusion a segmenti temporali: items = lista di segmenti/frame ID."""
    name = "vision_video"
    default_chunk, default_overlap = 48, 4
