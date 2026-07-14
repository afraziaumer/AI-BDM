"""Step 1 — Chunking.

Cut a document's `content` into small, overlapping notes.

Strategy:
  - Split on natural boundaries (paragraph -> line -> sentence -> word), never
    mid-word, using a recursive character splitter.
  - ~700 chars per chunk so it fits the embedding model's ~256-token window.
  - Overlap ~120 chars so a fact split across a boundary isn't lost.
  - Prepend the page title to each chunk so it keeps its context when embedded.
"""

from __future__ import annotations

from typing import List
from urllib.parse import urlparse

from . import config
from .contract import Chunk, SourceDoc

# Separator hierarchy: try to break on the biggest natural unit first.
_SEPARATORS = ["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ", ""]


def _domain_of(url: str) -> str:
    """Light domain extraction for per-business filtering (no external deps)."""
    return urlparse(url).netloc.lower().removeprefix("www.")


def _recursive_split(text: str, separators: List[str], size: int) -> List[str]:
    """Break text into pieces each <= size, preferring higher-level separators."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    sep = separators[0]
    rest = separators[1:]
    if sep == "":                       # last resort: hard character cut
        return [text[i:i + size] for i in range(0, len(text), size)]
    if sep not in text:                 # this separator doesn't help; go finer
        return _recursive_split(text, rest, size)

    pieces: List[str] = []
    for part in text.split(sep):
        part = part.strip()
        if not part:
            continue
        if len(part) <= size:
            pieces.append(part)
        else:                           # still too big — split it finer
            pieces.extend(_recursive_split(part, rest, size))
    return pieces


def _merge_with_overlap(pieces: List[str], size: int, overlap: int) -> List[str]:
    """Greedily merge small pieces into ~size chunks, carrying `overlap` chars
    of the previous chunk's tail into the next (so context survives)."""
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0

    for p in pieces:
        add = len(p) + (1 if cur else 0)
        if cur and cur_len + add > size:
            chunks.append(" ".join(cur))
            # Build the overlap tail from the end of the chunk we just closed.
            tail: List[str] = []
            tail_len = 0
            for q in reversed(cur):
                if tail_len + len(q) + 1 > overlap:
                    break
                tail.insert(0, q)
                tail_len += len(q) + 1
            cur = tail
            cur_len = sum(len(x) + 1 for x in cur)
        cur.append(p)
        cur_len += add

    if cur:
        chunks.append(" ".join(cur))
    return chunks


def chunk(doc: SourceDoc) -> List[Chunk]:
    """Split one SourceDoc into a list of Chunks (title prepended for context)."""
    content = (doc.content or "").strip()
    if not content:
        return []

    size = config.CHUNK_SIZE_CHARS
    overlap = config.CHUNK_OVERLAP_CHARS

    # Blank-line paragraph breaks are a HARD boundary, never re-merged back
    # together even if both sides would fit under `size` combined. Without
    # this, a short, distinctive fact sitting right after an unrelated block
    # (e.g. a contact/footer section: phone, email, hours) gets merged into
    # the same chunk purely because the whole thing is still small -- diluting
    # its semantic embedding with unrelated text, exactly the kind of bug
    # already fixed once for whole-page-sized content (see config.py's
    # CHUNK_SIZE_CHARS history). Splitting on "\n\n" first, and merging only
    # WITHIN each resulting paragraph, keeps every distinct block (contact
    # info, a product listing, a standalone fact) intact and independently
    # retrievable, instead of deleting anything.
    bodies: List[str] = []
    for para in content.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        pieces = _recursive_split(para, _SEPARATORS[1:], size)  # already split on "\n\n"
        bodies.extend(_merge_with_overlap(pieces, size, overlap))

    domain = doc.domain or _domain_of(doc.url)
    title = (doc.title or "").strip()
    chunks: List[Chunk] = []
    for i, body in enumerate(bodies):
        text = f"{title}\n\n{body}" if title else body
        chunks.append(Chunk(
            text=text,
            url=doc.url,
            title=title,
            chunk_no=i,
            domain=domain,
            chunk_id=f"{doc.url}#{i}",
        ))
    return chunks
