"""BGE-m3-ko 문서·질문 임베딩. 패키지의 로컬 스냅샷을 읽는다."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from .device import resolve_device


class SentenceEncoder:
    """첫 사용 때 모델을 한 번만 올리고, 스레드풀 요청이 동시에 GPU를 쓰지 않게 잠근다."""

    def __init__(self, model_path: Path, device: str = "auto", max_length: int = 1024, batch_size: int = 32):
        self.model_path = Path(model_path)
        self.name = self.model_path.name
        self.device = device
        self.max_length = max_length
        self.batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()

    def load(self):
        """모델 폴더가 없으면 허브 ID로 오인해 내려받지 않도록 바로 실패한다."""
        with self._lock:
            return self._load()

    def _load(self):
        if self._model is None:
            if not self.model_path.is_dir():
                raise FileNotFoundError(f"embedding model folder not found: {self.model_path}")
            import torch
            from sentence_transformers import SentenceTransformer

            device = resolve_device(self.device)
            dtype = torch.float16 if device == "cuda" else torch.float32
            model = SentenceTransformer(str(self.model_path), device=device, model_kwargs={"dtype": dtype})
            model.max_seq_length = self.max_length
            self._model = model
        return self._model

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        """정규화된 문서 벡터 (문서 수, 1024)를 반환한다."""
        with self._lock:
            vectors = self._load().encode_document(list(texts), batch_size=self.batch_size, normalize_embeddings=True,
                                                   convert_to_numpy=True, show_progress_bar=False)
        return np.asarray(vectors, dtype="float32")

    def encode_query(self, text: str) -> np.ndarray:
        """정규화된 질문 벡터 (1024,)를 반환한다."""
        with self._lock:
            vectors = self._load().encode_query([text], normalize_embeddings=True,
                                                convert_to_numpy=True, show_progress_bar=False)
        return np.asarray(vectors[0], dtype="float32")
