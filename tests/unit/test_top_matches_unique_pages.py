"""Tests for rag.top_matches._dedup_by_page() -- the unique_pages=True
top-k diversity constraint.

Feature added after a real gap found live: a single content-farm domain
with dozens of loosely on-topic articles took ALL 15 "top website chunk"
slots in a real run (sometimes several chunks from the very same article),
leaving zero room for the run's other 8 real businesses even though they
had their own, more specific matches. _print_ranked_chunks (the actual
"top 15" display in ingest_and_answer.py) now passes unique_pages=True so
each of the k slots represents a distinct page. The "scan every chunk"
per-business evidence summary caller deliberately does NOT set this --
it wants every matching chunk, not a deduped top-k.

Run: pytest -q tests/unit/test_top_matches_unique_pages.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from rag.top_matches import _dedup_by_page  # noqa: E402


def _result(url: str, score: float, chunk_no: int = 0) -> dict:
    return {"url": url, "score": score, "chunk_no": chunk_no}


def test_keeps_only_the_best_chunk_per_page():
    # Already sorted best-first, as top_matches() guarantees before calling this.
    results = [
        _result("https://a.com/page1", 0.95, chunk_no=3),
        _result("https://a.com/page1", 0.90, chunk_no=7),  # same page, lower score -- dropped
        _result("https://a.com/page2", 0.85),
        _result("https://b.com/page1", 0.80),
    ]
    out = _dedup_by_page(results, k=10)
    urls = [r["url"] for r in out]
    assert urls == ["https://a.com/page1", "https://a.com/page2", "https://b.com/page1"]
    assert out[0]["chunk_no"] == 3  # the higher-scoring of the two page1 chunks


def test_caps_at_k_distinct_pages():
    results = [_result(f"https://x.com/p{i}", 1.0 - i * 0.01) for i in range(20)]
    out = _dedup_by_page(results, k=15)
    assert len(out) == 15
    assert len({r["url"] for r in out}) == 15


def test_a_single_dominant_domain_cannot_fill_every_slot():
    """The exact real-world scenario: one content-farm domain with 20
    chunks across only 3 distinct pages must not crowd out other domains
    that appear later in the sorted (lower-scoring) list."""
    dominant = [_result(f"https://spam.com/article-{i % 3}", 0.99 - i * 0.001) for i in range(20)]
    real_business = [_result("https://real-restaurant.com/", 0.5)]
    out = _dedup_by_page(dominant + real_business, k=15)
    urls = [r["url"] for r in out]
    assert len(urls) == 4  # only 3 distinct spam pages + 1 real business
    assert "https://real-restaurant.com/" in urls


def test_no_duplicate_urls_ever_in_output():
    results = [_result("https://same.com/", float(i)) for i in range(50)]
    out = _dedup_by_page(results, k=15)
    assert len(out) == 1  # only one distinct URL exists at all
