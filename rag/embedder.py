"""Step 2 — Embedding.

Turn chunk text into 384-dim meaning-vectors with all-MiniLM-L6-v2.

Strategy:
  - Load the model ONCE (expensive to load, cheap to reuse).
  - Encode in batches for speed.
  - Normalize vectors so cosine similarity == dot product downstream.
"""

from __future__ import annotations

import os
import time
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
            # This step (importing torch/transformers + loading the model)
            # is the single slowest, most opaque part of a RAG run — it can
            # take anywhere from ~2s (warm OS/AV file cache) to 1-2+ minutes
            # (cold cache, antivirus scanning each file). With no visible
            # output during that window it looks identical to a genuine
            # hang, which is exactly what caused a long, confusing debugging
            # session. Printing before/after with elapsed time removes that
            # ambiguity — silence for over ~2 minutes AFTER this message
            # would mean something is actually wrong; before it, it doesn't.
            print(
                "[embedder] Loading the embedding model (sentence-transformers/"
                f"{self._model_name.rsplit('/', 1)[-1]}) — first use this run, "
                "can take a few seconds to ~2 minutes depending on disk/"
                "antivirus cache state. Not stuck; just loading."
            )
            _t0 = time.time()
            # Imported here so simply importing this module is cheap and doesn't
            # require torch to be installed until you actually embed.
            from sentence_transformers import SentenceTransformer
            # A normal (non-offline) load makes a live network round-trip to
            # the HF Hub to check for a newer revision EVERY time, even when
            # the weights are already cached locally — confirmed live via a
            # stack-trace capture: stuck for 2+ minutes on an SSL socket read
            # inside huggingface_hub.hf_hub_download -> get_tokenizer_config,
            # on a machine where the model was already fully cached (the
            # weight load itself completes in well under a second). The old
            # fix here only set HF_HUB_OFFLINE=1 AFTER this line succeeded,
            # which protects a second Embedder() in the SAME process but does
            # nothing for the very first one — which is the only one that
            # matters for typical CLI usage, since every invocation is a
            # fresh process. Fixed by trying local_files_only=True FIRST
            # (zero network calls when cached, near-instant); only a
            # genuinely uncached model (first-ever run on this machine)
            # falls back to the network-enabled path below.
            try:
                self._model = SentenceTransformer(self._model_name, local_files_only=True)
            except Exception:
                self._model = SentenceTransformer(self._model_name)
            os.environ["HF_HUB_OFFLINE"] = "1"
            print(f"[embedder] Model ready in {time.time() - _t0:.1f}s.")

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
