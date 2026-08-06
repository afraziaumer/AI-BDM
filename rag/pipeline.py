"""Orchestration — wire the stages into two clean entry points.

  ingest(doc)            : chunk -> embed -> store   (build the knowledge base)
  answer(question, biz)  : retrieve -> generate      (query the knowledge base)

Thin by design: it just connects chunker / embedder / store / retriever /
generator. All real logic lives in those modules. Imports nothing outside rag/.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import config, generator
from .chunker import chunk
from .contract import SourceDoc
from .embedder import Embedder
from .retriever import retrieve
from .store import VectorStore, get_store


class RagPipeline:
    """Holds the shared model + store so they load once and are reused."""

    def __init__(self) -> None:
        self.embedder = Embedder()
        # Load PyTorch (the embedding model) BEFORE the vector store. On Windows,
        # chromadb pulls in onnxruntime, which conflicts with a later torch load
        # and segfaults the process. Loading torch first avoids the crash.
        self.embedder.warmup()
        self.store: VectorStore = get_store()

    def ingest(self, doc: SourceDoc) -> int:
        """Chunk -> embed -> store one document. Returns #chunks added."""
        chunks = chunk(doc)
        if not chunks:
            return 0
        vectors = self.embedder.embed([c.text for c in chunks])
        for c, v in zip(chunks, vectors):
            c.embedding = v
        self.store.add(chunks)
        return len(chunks)

    def answer(self, question: str, business: Optional[str] = None,
               k: int = config.TOP_K) -> Dict[str, Any]:
        """Retrieve -> generate an answer for a question."""
        chunks = retrieve(question, self.store, self.embedder,
                          business=business, k=k)
        if not chunks:
            return {"answer": "Not stated on the website.", "sources": [],
                    "chunks_used": 0}
        result = generator.answer(question, chunks)
        result["chunks_used"] = len(chunks)
        return result
