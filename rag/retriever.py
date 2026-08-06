"""Step 4 — Retrieval (the "R" in RAG).

Given a question, return the top-k most relevant chunks.

Strategy:
  - Embed the QUESTION with the same model used for chunks (same vector space).
  - Search the store for the closest chunks.
  - Filter by business `domain` so you answer about the RIGHT company.
"""

from __future__ import annotations

from typing import List, Optional

from . import config
from .contract import Chunk
from .embedder import Embedder
from .store import VectorStore


def retrieve(question: str, store: VectorStore, embedder: Embedder,
             business: Optional[str] = None, k: int = config.TOP_K) -> List[Chunk]:
    """Return the k chunks most relevant to `question`.

    `business` is a registered domain (e.g. "sachardental.com"); when given,
    results are restricted to that company's chunks.
    """
    if not question.strip():
        return []
    query_vector = embedder.embed_one(question)
    where = {"domain": business.lower().removeprefix("www.")} if business else None
    return store.search(query_vector, k=k, where=where)
