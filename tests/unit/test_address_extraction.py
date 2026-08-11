"""Tests for phase1_pipeline._ADDRESS_RE -- the free-text fallback address
pattern (used only when schema.org/microdata/<address>/hCard extraction all
find nothing). Pure regex, no network.

Run: pytest -q tests/unit/test_address_extraction.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import phase1_pipeline as p1  # noqa: E402


def test_matches_standard_us_style_address():
    m = p1._ADDRESS_RE.search("123 Main St, New York, NY 10001")
    assert m is not None


def test_matches_comma_directly_after_house_number():
    """'Office No 14, Ground Floor' -- a real South Asian convention where
    the house number is followed directly by a comma, not whitespace."""
    m = p1._ADDRESS_RE.search("Office No 14, Ground Floor, Blue Area, Islamabad")
    assert m is not None


def test_matches_letter_suffixed_plot_number():
    """'17-E G-10 Markaz' -- a real South Asian convention for a
    letter-suffixed plot/house number."""
    m = p1._ADDRESS_RE.search("17-E G-10 Markaz, Islamabad")
    assert m is not None


def test_matches_street_suffix_word_immediately_after_house_number():
    """Real bug found while executing a professional QA test suite
    (REG-008): '8 Markaz, Islamabad 44000' means house #8 IN "Markaz"
    itself, with no filler words between the number and the suffix word --
    a valid, common South Asian pattern. The regex previously required at
    least 2 filler characters between the house number and the
    street-suffix word ({2,45}?), which rejected this case outright.
    Fixed by allowing zero filler characters ({0,45}?)."""
    m = p1._ADDRESS_RE.search("8 Markaz, Islamabad 44000")
    assert m is not None
    assert m.group(0).startswith("8 Markaz")


def test_does_not_match_a_bare_number_with_no_street_suffix_at_all():
    """A number with no recognizable street-suffix word anywhere nearby
    should not match -- this isn't an address, it's noise (e.g. a phone
    number fragment or a price)."""
    m = p1._ADDRESS_RE.search("Call us at 12345 for more information today")
    assert m is None
