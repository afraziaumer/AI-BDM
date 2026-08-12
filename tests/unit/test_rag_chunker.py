"""Tests for rag/chunker.py -- previously untested in isolation.

Run: pytest -q tests/unit/test_rag_chunker.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from rag.chunker import chunk  # noqa: E402
from rag.contract import SourceDoc  # noqa: E402


def _doc(content, url="https://acme.com/pricing", title="Pricing", **kw):
    return SourceDoc(url=url, title=title, content=content, **kw)


def test_empty_content_produces_no_chunks():
    assert chunk(_doc("")) == []
    assert chunk(_doc("   ")) == []


def test_short_content_produces_one_chunk_with_title_prepended():
    chunks = chunk(_doc("We offer dental cleaning and checkups."))
    assert len(chunks) == 1
    assert chunks[0].text.startswith("Pricing\n\n")
    assert "dental cleaning" in chunks[0].text


def test_long_content_is_split_into_multiple_chunks():
    long_text = " ".join([f"Sentence number {i} about our services." for i in range(60)])
    chunks = chunk(_doc(long_text))
    assert len(chunks) > 1
    assert all(c.chunk_no == i for i, c in enumerate(chunks))


def test_paragraph_boundaries_are_never_merged_back_together():
    """A short, distinct paragraph (e.g. contact info) must stay its own
    chunk rather than being merged with an unrelated preceding paragraph
    just because the combined size would still fit under the cap."""
    content = "Short intro paragraph.\n\nPhone: 555-1234\nEmail: x@acme.com\nHours: 9-5"
    chunks = chunk(_doc(content))
    texts = [c.text for c in chunks]
    assert any("Phone: 555-1234" in t and "Short intro paragraph" not in t for t in texts)


def test_chunk_ids_are_unique_and_reference_the_source_url():
    long_text = " ".join([f"Fact {i} about the business." for i in range(40)])
    chunks = chunk(_doc(long_text, url="https://acme.com/about"))
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))
    assert all(cid.startswith("https://acme.com/about#") for cid in ids)


def test_domain_defaults_to_url_derived_when_not_supplied():
    chunks = chunk(_doc("Some content here.", url="https://www.acme.com/pricing"))
    assert chunks[0].domain == "acme.com"  # www. stripped


def test_explicit_domain_is_preserved_over_url_derived():
    chunks = chunk(_doc("Some content here.", url="https://cdn.acme.com/x", domain="acme.com"))
    assert chunks[0].domain == "acme.com"


def test_review_source_type_is_preserved_on_every_chunk():
    long_text = " ".join([f"Review fact {i}." for i in range(30)])
    chunks = chunk(_doc(long_text, source_type="review"))
    assert all(c.source_type == "review" for c in chunks)


def test_no_chunk_exceeds_a_reasonable_size_bound():
    """Chunks aren't split mid-word and stay near the configured cap -- a
    single pathologically long unbroken "word" still gets hard-cut so no
    chunk grows unboundedly."""
    long_text = "supercalifragilisticexpialidocious " * 200
    chunks = chunk(_doc(long_text))
    from rag import config
    # Allow generous slack for title-prepend + merge overlap, but never
    # anywhere near unbounded.
    assert all(len(c.text) < config.CHUNK_SIZE_CHARS * 3 for c in chunks)
