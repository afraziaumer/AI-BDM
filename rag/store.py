"""Step 3 — Vector storage.

Save {text, vector, url, title, chunk_no, domain} and search by meaning.

Strategy:
  - Program against the abstract VectorStore interface, NOT a specific DB.
  - Develop on ChromaStore (local, zero setup) today.
  - Swap to MongoStore (Atlas Vector Search) later by config only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from . import config
from .contract import Chunk


class VectorStore(ABC):
    """The one interface every backend implements."""

    @abstractmethod
    def add(self, chunks: List[Chunk]) -> None:
        """Persist chunks (each must already carry .embedding)."""

    @abstractmethod
    def search(self, query_vector: List[float], k: int = config.TOP_K,
               where: Optional[Dict[str, Any]] = None) -> List[Chunk]:
        """Return the k most similar chunks, optionally filtered (e.g. by domain)."""

    @abstractmethod
    def count(self) -> int:
        """Number of chunks currently stored."""


class ChromaStore(VectorStore):
    """Local dev store — persists to disk under config.CHROMA_DIR. No server."""

    def __init__(self) -> None:
        import chromadb
        self._client = chromadb.PersistentClient(path=config.CHROMA_DIR)
        # cosine space to match our normalized embeddings.
        self._col = self._client.get_or_create_collection(
            name=config.CHROMA_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, chunks: List[Chunk]) -> None:
        if not chunks:
            return
        self._col.upsert(
            ids=[c.chunk_id or f"{c.url}#{c.chunk_no}" for c in chunks],
            embeddings=[c.embedding for c in chunks],
            documents=[c.text for c in chunks],
            metadatas=[c.as_metadata() for c in chunks],
        )

    def search(self, query_vector, k=config.TOP_K, where=None) -> List[Chunk]:
        res = self._col.query(
            query_embeddings=[query_vector],
            n_results=k,
            where=where or None,
        )
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        out: List[Chunk] = []
        for text, meta in zip(docs, metas):
            meta = meta or {}
            out.append(Chunk(
                text=text,
                url=meta.get("url", ""),
                title=meta.get("title", ""),
                chunk_no=int(meta.get("chunk_no", 0) or 0),
                domain=meta.get("domain", ""),
                chunk_id=meta.get("chunk_id", ""),
            ))
        return out

    def count(self) -> int:
        return self._col.count()


class MongoStore(VectorStore):
    """Production store — MongoDB Atlas Vector Search. (implement in M6)

    Sketch:
      - pymongo client to config.MONGO_URI / MONGO_DB / MONGO_COLLECTION.
      - Create a vector search index (numDimensions=384, similarity="cosine").
      - add(): insert_many docs {text, embedding, url, title, chunk_no, domain}.
      - search(): aggregate with a $vectorSearch stage, optional domain filter.
    """

    def __init__(self) -> None:
        raise NotImplementedError("MongoStore — implement in M6 (Atlas setup)")

    def add(self, chunks: List[Chunk]) -> None:
        raise NotImplementedError

    def search(self, query_vector, k=config.TOP_K, where=None) -> List[Chunk]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError


def get_store() -> VectorStore:
    """Factory: return the store selected in config.STORE_BACKEND."""
    if config.STORE_BACKEND == "chroma":
        return ChromaStore()
    if config.STORE_BACKEND == "mongo":
        return MongoStore()
    raise ValueError(f"Unknown STORE_BACKEND: {config.STORE_BACKEND}")
