"""Tests for page_patterns.detect_patterns()'s multi-depth prefix search.

Real defect found live: a real OTA homepage (priceline.com) has repeating
listing links like "/hotel-deals/en-us/<region-id>/hotels-in-<city>.ssp",
where BOTH the id and the human-readable slug vary together. The original
single-level grouping ("parent = everything except the last segment") gave
each link a unique parent (since the id, one level up, differs every time),
so 10 obviously-templated links produced zero detected patterns.

Run: pytest -q tests/unit/test_page_patterns_deep_prefix.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from page_patterns import detect_patterns  # noqa: E402


def _candidate(url: str, anchor: str = "") -> dict:
    return {"url": url, "anchor": anchor, "location": "nav"}


def test_single_level_pattern_still_detected_unchanged():
    """The ORIGINAL, simplest case (only the last segment varies) must
    keep working exactly as before -- this is the deepest/most-specific
    prefix, tried first."""
    candidates = [_candidate(f"https://example.com/boats/boat-{i:03d}") for i in range(6)]
    individual, patterns = detect_patterns(candidates)
    assert len(patterns) == 1
    assert patterns[0].pattern == "/boats/*"
    assert patterns[0].count == 6
    assert individual == []


def test_two_segment_varying_pattern_is_now_detected():
    """The real bug case: BOTH the id and the slug vary together, so no
    two candidates share the same immediate parent -- must fall back to a
    shallower, shared prefix."""
    ids = ["CA", "CAON", "P3000007712", "P3000014599", "P3000008227",
           "P3000021455", "P3000001534", "P3000012270", "P3000003361", "P3000001349"]
    slugs = ["hotels-in-canada", "hotels-in-ontario", "hotels-in-florence",
              "hotels-in-cranbury", "hotels-in-chalmette", "hotels-in-lubbock",
              "hotels-in-calabasas", "hotels-in-jackson", "hotels-in-palm-coast",
              "hotels-in-phoenix"]
    candidates = [
        _candidate(f"https://example-ota.com/hotel-deals/en-us/{i}/{s}.ssp")
        for i, s in zip(ids, slugs)
    ]
    individual, patterns = detect_patterns(candidates)
    assert len(patterns) == 1
    assert patterns[0].pattern == "/hotel-deals/en-us/*"
    assert patterns[0].count == 10
    assert individual == []


def test_unrelated_single_pages_are_not_grouped():
    candidates = [
        _candidate("https://example.com/about"),
        _candidate("https://example.com/contact"),
        _candidate("https://example.com/menu"),
    ]
    individual, patterns = detect_patterns(candidates)
    assert patterns == []
    assert len(individual) == 3


def test_below_threshold_group_stays_individual():
    # Only 3 items -- PATTERN_MIN_CHILDREN is 4, so this must NOT form a pattern.
    candidates = [_candidate(f"https://example.com/boats/boat-{i}") for i in range(3)]
    individual, patterns = detect_patterns(candidates)
    assert patterns == []
    assert len(individual) == 3
