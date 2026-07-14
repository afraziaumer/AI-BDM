"""The data shapes that flow through the RAG layer.

`SourceDoc` is the ONLY thing this package accepts from the outside world
(Developer A's output). Depending on this tiny schema — not on HTML — is what
keeps the two halves of the project independent.

`Chunk` is what we produce internally after splitting a document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SourceDoc:
    """The shared JSON contract from Developer A (Website Processing).

    `domain` is OPTIONAL and defaults to "" — when the caller already knows the
    authoritative registered domain (e.g. from the scraper's crawl_index.csv,
    which computes it once via tldextract), pass it here so chunker.py doesn't
    need to re-derive a possibly-less-accurate domain from the URL itself.
    Leaving it "" preserves the old URL-derived behavior exactly.
    """
    url: str
    title: str
    content: str
    domain: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "SourceDoc":
        return cls(
            url=d.get("url", "") or "",
            title=d.get("title", "") or "",
            content=d.get("content", "") or "",
            domain=d.get("domain", "") or "",
        )


@dataclass
class Chunk:
    """One small piece of a document, ready to embed and store."""
    text: str                      # the chunk text (title is prepended for context)
    url: str                       # source page it came from
    title: str                     # source page title
    chunk_no: int                  # 0-based index within the document
    domain: str = ""               # registered domain, for per-business filtering
    embedding: Optional[List[float]] = None   # filled in by the embedder
    chunk_id: str = ""             # unique id (e.g. f"{url}#{chunk_no}")

    def as_metadata(self) -> dict:
        """Metadata to store alongside the vector (no embedding here)."""
        return {
            "url": self.url,
            "title": self.title,
            "chunk_no": self.chunk_no,
            "chunk_id": self.chunk_id,
            "domain": self.domain,
        }
