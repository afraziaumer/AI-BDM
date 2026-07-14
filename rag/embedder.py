"""Step 2 — Embedding.

Turn chunk text into 384-dim meaning-vectors with all-MiniLM-L6-v2.

Strategy:
  - Load the model ONCE (expensive to load, cheap to reuse).
  - Encode in batches for speed.
  - Normalize vectors so cosine similarity == dot product downstream.
"""

from __future__ import annotations

import os
from typing import List

from . import config

# Windows: torch + onnxruntime (via chromadb) ship duplicate OpenMP runtimes,
# which can abort the process. This env var is the standard, safe workaround.
# Set before torch is imported.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


class Embedder:
    """Wraps the sentence-transformers model. Load once, reuse everywhere."""

    def __init__(self, model_name: str = config.EMBEDDING_MODEL) -> None:
        self._model_name = model_name
        self._model = None  # lazy-loaded on first embed()

    def _ensure_model(self) -> None:
        if self._model is None:
            # Imported here so simply importing this module is cheap and doesn't
            # require torch to be installed until you actually embed.
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)

    def warmup(self) -> None:
        """Load the model now. Call this BEFORE creating a Chroma store — on
        Windows, PyTorch must initialize before onnxruntime (pulled in by
        chromadb) or the process segfaults due to a native-library conflict."""
        self._ensure_model()

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Return one vector per input text."""
        if not texts:
            return []
        self._ensure_model()
        vectors = self._model.encode(
            texts,
            batch_size=32,
            normalize_embeddings=config.NORMALIZE_EMBEDDINGS,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    def embed_one(self, text: str) -> List[float]:
        """Convenience for embedding a single string (e.g. a query)."""
        return self.embed([text])[0]
