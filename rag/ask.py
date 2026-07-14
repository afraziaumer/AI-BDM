"""Query the RAG knowledge base from the command line.

Run (after ingesting):
  python -m rag.ask "does this dentist offer implants?" --business sachardental.com
  python -m rag.ask "what services are offered?"        # search across all
"""

from __future__ import annotations

import argparse
import sys

from . import config


def main() -> None:
    # Windows consoles default to cp1252 and crash when printing characters like
    # a non-breaking hyphen. Force UTF-8 output where supported.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Ask the RAG knowledge base a question.")
    parser.add_argument("question", help="The question to answer.")
    parser.add_argument("--business", default=None,
                        help="Restrict to one company's domain (e.g. sachardental.com).")
    parser.add_argument("--k", type=int, default=config.TOP_K,
                        help="How many chunks to retrieve.")
    args = parser.parse_args()

    from .pipeline import RagPipeline
    pipe = RagPipeline()
    result = pipe.answer(args.question, business=args.business, k=args.k)

    print("\n" + "=" * 60)
    print("Q:", args.question)
    if args.business:
        print("Business:", args.business)
    print("-" * 60)
    print(result["answer"])
    print("-" * 60)
    print(f"(used {result.get('chunks_used', 0)} chunks)")
    for s in result.get("sources", []):
        print("  source:", s)
    print("=" * 60)


if __name__ == "__main__":
    main()
