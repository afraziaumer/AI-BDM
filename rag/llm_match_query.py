"""Find the best CSV chunk for a query, then ask the LLM to explain it.

Run:
  python -m rag.llm_match_query "what dental services are offered?"
  python -m rag.llm_match_query "battery backup" --path rag/dummy_source_docs.csv
"""

from __future__ import annotations

import argparse
import sys

from . import config, generator
from .match_query import _shorten, match_query


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Match a query to the best CSV chunk and ask the LLM to explain it."
    )
    parser.add_argument("query", help="The query to match and explain.")
    parser.add_argument("--path", default=config.RAW_STORE_CSV, help="Source CSV.")
    args = parser.parse_args()

    matches = match_query(args.query, path=args.path, k=1)

    print("\n" + "=" * 70)
    print("Query:", args.query)
    print("CSV:", args.path)
    print("-" * 70)

    if not matches:
        print("No matching chunks found.")
        print("=" * 70)
        return

    score, best_chunk = matches[0]
    result = generator.explain_chunk_match(args.query, best_chunk, score)

    print(f"Best match score: {score:.4f}")
    print("Title:", best_chunk.title)
    print("URL:", best_chunk.url)
    print("Chunk:", best_chunk.chunk_no)
    print("-" * 70)
    print("Matched chunk preview:")
    print(_shorten(best_chunk.text))
    print("-" * 70)
    print("LLM explanation:")
    print(result["analysis"])
    print("=" * 70)


if __name__ == "__main__":
    main()
