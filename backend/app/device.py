"""로컬 모델을 올릴 장치 선택. torch는 실제 모델을 쓸 때만 import한다."""


def resolve_device(device: str) -> str:
    """'auto'면 CUDA가 있을 때 cuda, 아니면 cpu를 고른다. 명시한 값은 그대로 쓴다."""
    if device != "auto":
        return device
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"
