"""Tests for website_classifier.rule_based_score()'s pattern_max_size signal.

Real defect found live: an OTA-style homepage (e.g. priceline.com) with
ONE repeating URL-listing pattern absorbing all 10 of its listing links
(rather than 3+ DISTINCT patterns) scored only 15 -- landing exactly at
DIRECTORY_SCORE_LOW, which confidently (and wrongly) classified it as a
single official business with deep_crawl=True and no LLM safety net at
all. A single pattern with many children is just as strong a directory
signal as several smaller distinct ones.

Run: pytest -q tests/unit/test_website_classifier_pattern_size.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import website_classifier as wc  # noqa: E402

_OTA_HOMEPAGE = """
<html><head><title>Hotel Deals - Compare Prices and Book Hotels Worldwide</title>
<meta name="description" content="Search hotels, compare prices, and book the best hotel deals in cities worldwide.">
</head><body>
<nav>
<a href="/hotel-deals/en-us/CA/hotels-in-canada.ssp">Hotels in Canada</a>
<a href="/hotel-deals/en-us/CAON/hotels-in-ontario.ssp">Hotels in Ontario</a>
<a href="/hotel-deals/en-us/P3000007712/hotels-in-florence.ssp">Hotels in Florence</a>
<a href="/hotel-deals/en-us/P3000014599/hotels-in-cranbury.ssp">Hotels in Cranbury</a>
<a href="/hotel-deals/en-us/P3000008227/hotels-in-chalmette.ssp">Hotels in Chalmette</a>
<a href="/hotel-deals/en-us/P3000021455/hotels-in-lubbock.ssp">Hotels in Lubbock</a>
<a href="/hotel-deals/en-us/P3000001534/hotels-in-calabasas.ssp">Hotels in Calabasas</a>
<a href="/hotel-deals/en-us/P3000012270/hotels-in-jackson.ssp">Hotels in Jackson</a>
<a href="/hotel-deals/en-us/P3000003361/hotels-in-palm-coast.ssp">Hotels in Palm Coast</a>
<a href="/hotel-deals/en-us/P3000001349/hotels-in-phoenix.ssp">Hotels in Phoenix</a>
</nav>
<h1>Find and Compare the Best Hotel Deals</h1>
</body></html>
"""


def test_ota_homepage_no_longer_lands_at_confidently_official():
    signals = wc.extract_homepage_signals(_OTA_HOMEPAGE, "https://example-ota.com/", "example-ota.com", {})
    score, _reasons = wc.rule_based_score(signals)
    assert score > wc.DIRECTORY_SCORE_LOW, (
        f"score={score} would confidently (and wrongly) classify this OTA homepage "
        "as a single official business with no LLM safety net"
    )


def test_pattern_max_size_signal_fires_with_reason():
    signals = wc.extract_homepage_signals(_OTA_HOMEPAGE, "https://example-ota.com/", "example-ota.com", {})
    assert signals.pattern_max_size == 10
    _score, reasons = wc.rule_based_score(signals)
    assert any("repeating URL-listing pattern" in r for r in reasons)


def test_small_pattern_below_threshold_does_not_trigger_the_bonus():
    html = """
    <html><head><title>Joe's Diner</title></head><body>
    <nav>
    <a href="/menu/burgers">Burgers</a>
    <a href="/menu/salads">Salads</a>
    <a href="/menu/drinks">Drinks</a>
    </nav>
    </body></html>
    """
    signals = wc.extract_homepage_signals(html, "https://joesdiner.com/", "joesdiner.com", {})
    score, reasons = wc.rule_based_score(signals)
    assert not any("repeating URL-listing pattern" in r for r in reasons)
    assert score <= wc.DIRECTORY_SCORE_LOW


def test_three_distinct_patterns_still_use_the_original_pattern_count_path():
    """Unaffected regression check: the pre-existing pattern_count >= 3
    branch must still fire for its own original shape (several DISTINCT
    smaller patterns), not the new pattern_max_size branch."""
    html_parts = ["<html><head><title>Big Directory</title></head><body><nav>"]
    for section in ("boats", "marinas", "yachts"):
        for i in range(4):
            html_parts.append(f'<a href="/{section}/{section}-{i}">{section} {i}</a>')
    html_parts.append("</nav></body></html>")
    html = "".join(html_parts)
    signals = wc.extract_homepage_signals(html, "https://example.com/", "example.com", {})
    assert signals.pattern_count == 3
    score, reasons = wc.rule_based_score(signals)
    assert any("distinct repeating URL-listing patterns" in r for r in reasons)
