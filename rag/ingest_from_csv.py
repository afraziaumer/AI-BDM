"""Dev helper: feed the RAG layer from a CSV file.

The default CSV is rag/dummy_source_docs.csv and uses the simple contract:
  url,title,content

Scraper-style CSVs are still supported too:
  page_url,page_title,page_text

Run:
  python -m rag.ingest_from_csv
  python -m rag.ingest_from_csv --path scavenger_leads_cache.csv --limit 20
"""

from __future__ import annotations

import argparse
import csv
import sys
from typing import Iterator

from . import config
from .contract import SourceDoc

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def iter_source_docs(path: str = config.RAW_STORE_CSV) -> Iterator[SourceDoc]:
    """Yield one SourceDoc per CSV row that has text.

    Supports both the simple dummy contract columns:
      url, title, content

    and the scraper output columns:
      page_url, page_title, page_text
    """
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            content = (row.get("content") or row.get("page_text") or "").strip()
            if not content:
                continue
            yield SourceDoc(
                url=row.get("url") or row.get("page_url") or "",
                title=row.get("title") or row.get("page_title") or "",
                content=content,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest CSV rows into the RAG store.")
    parser.add_argument("--path", default=config.RAW_STORE_CSV, help="Source CSV.")
    parser.add_argument("--limit", type=int, default=0, help="Max pages (0 = all).")
    args = parser.parse_args()

    from .pipeline import RagPipeline
    pipe = RagPipeline()

    pages = 0
    total_chunks = 0
    for doc in iter_source_docs(args.path):
        total_chunks += pipe.ingest(doc)
        pages += 1
        if pages % 20 == 0:
            print(f"  ingested {pages} pages / {total_chunks} chunks...")
        if args.limit and pages >= args.limit:
            break

    print(f"[ingest] done: {pages} pages -> {total_chunks} chunks. "
          f"Store now holds {pipe.store.count()} chunks.")


if __name__ == "__main__":
    main()
