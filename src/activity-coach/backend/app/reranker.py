"""KURE-Reranker-nano cross-encoder. 질문·문서 쌍마다 관련도 점수를 매긴다."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from .device import resolve_device


class CrossEncoderReranker:
    """점수는 활성화 전 logit이다. fp16 sigmoid 포화로 생기는 동점을 피하려고 rag_test와 같게 둔다."""

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
                raise FileNotFoundError(f"reranker model folder not found: {self.model_path}")
            import torch
            from sentence_transformers import CrossEncoder

            device = resolve_device(self.device)
            dtype = torch.float16 if device == "cuda" else torch.float32
            model = CrossEncoder(str(self.model_path), device=device, max_length=self.max_length,
                                 model_kwargs={"dtype": dtype})
            model.activation_fn = torch.nn.Identity()
            self._model = model
        return self._model

    def score(self, query: str, documents: list[str]) -> list[float]:
        """문서 순서대로 raw logit 점수를 반환한다. 높을수록 질문과 관련이 크다."""
        if not documents:
            return []
        with self._lock:
            scores = self._load().predict([(query, document) for document in documents], batch_size=self.batch_size,
                                          convert_to_numpy=True, show_progress_bar=False)
        return [float(value) for value in np.asarray(scores, dtype="float32").reshape(-1)]
