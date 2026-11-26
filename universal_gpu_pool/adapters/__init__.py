def get_adapter(name):
    if name == "demo":
        from adapters.demo_adapter import DemoAdapter as A
    elif name == "spatial_3d":
        from adapters.spatial_3d_adapter import Spatial3DAdapter as A
    elif name == "vision_video":
        from adapters.vision_video_adapter import VisionVideoAdapter as A
    elif name == "llm_batch":
        from adapters.llm_batch_adapter import LLMBatchAdapter as A
    elif name == "training_finetune":
        from adapters.training_finetune_adapter import TrainingFinetuneAdapter as A
    elif name == "universal_model":
        from adapters.universal_model_adapter import UniversalModelAdapter as A
    else:
        raise KeyError(f"adapter sconosciuto: {name}")
    return A
