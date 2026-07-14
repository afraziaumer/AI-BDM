"""Match a user query directly against chunks from a CSV file.

This is a simple one-command retrieval demo:
  CSV rows -> chunks -> chunk embeddings -> query embedding -> best matches

Run:
  python -m rag.match_query "what dental services are offered?"
  python -m rag.match_query "battery backup" --path rag/dummy_source_docs.csv --k 5
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable, List, Tuple

from . import config
from .chunker import chunk
from .contract import Chunk
from .embedder import Embedder
from .ingest_from_csv import iter_source_docs


def _dot(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine score for normalized vectors."""
    return sum(x * y for x, y in zip(a, b))


def _shorten(text: str, max_chars: int = 600) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def match_query(query: str, path: str = config.RAW_STORE_CSV,
                k: int = config.TOP_K) -> List[Tuple[float, Chunk]]:
    """Return the top-k chunks from `path` that best match `query`."""
    all_chunks: List[Chunk] = []
    for doc in iter_source_docs(path):
        all_chunks.extend(chunk(doc))

    if not query.strip() or not all_chunks:
        return []

    embedder = Embedder()
    texts = [c.text for c in all_chunks]
    chunk_vectors = embedder.embed(texts)
    query_vector = embedder.embed_one(query)

    scored = [
        (_dot(query_vector, chunk_vector), source_chunk)
        for chunk_vector, source_chunk in zip(chunk_vectors, all_chunks)
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[:k]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Chunk a CSV, embed it, and show chunks closest to a query."
    )
    parser.add_argument("query", help="The query to match against the CSV text.")
    parser.add_argument("--path", default=config.RAW_STORE_CSV, help="Source CSV.")
    parser.add_argument("--k", type=int, default=config.TOP_K,
                        help="How many matching chunks to show.")
    args = parser.parse_args()

    matches = match_query(args.query, path=args.path, k=args.k)

    print("\n" + "=" * 70)
    print("Query:", args.query)
    print("CSV:", args.path)
    print("-" * 70)

    if not matches:
        print("No matching chunks found.")
    for rank, (score, source_chunk) in enumerate(matches, 1):
        print(f"[{rank}] score={score:.4f}")
        print("Title:", source_chunk.title)
        print("URL:", source_chunk.url)
        print("Chunk:", source_chunk.chunk_no)
        print(_shorten(source_chunk.text))
        print("-" * 70)

    print("=" * 70)


if __name__ == "__main__":
    main()
