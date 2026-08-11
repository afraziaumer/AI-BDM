"""Tests for discovery_classifier._looks_like_listicle()'s hyphen-slug
signal.

Real defect found live while investigating a real user query ("find 10
restaurants in Texas with no online reservation system"): a content-farm
blog (takemetotn.com) publishing "hidden gem" clickbait articles ("11
Texas Comfort Food Restaurants Only Locals Seem to Know About") was
committed as if it were itself a qualifying restaurant business -- its
company_name ended up being the article's TITLE, its industry field was
None, and its 8 "high-intent pages" were listicle articles about
restaurants in Texas, Delaware, New Jersey, and Tennessee. Because this
one domain had far more chunks than any real business, it went on to
dominate the RAG ranking's top-15 for both website and review chunks,
crowding out the other 8 real businesses entirely.

Root cause: the title didn't match any "N best/top X" or "X in Y" phrasing
in LISTICLE_TITLE_RE, and the path didn't contain any of LISTICLE_PATH_HINTS
(no "/blog", "/guide", etc.) -- but the URL slug itself is unmistakably an
article: long and hyphen-heavy. Fixed by reusing the same 5+-hyphens signal
already proven for filtering blog posts during internal-link crawling (see
phase1_pipeline.py's _is_internal_crawl_noise).

Run: pytest -q tests/unit/test_discovery_classifier_listicle.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import discovery_classifier as dc  # noqa: E402


def test_real_bug_url_is_now_a_discovery_source():
    url = "https://takemetotn.com/11-texas-comfort-food-restaurants-only-locals-seem-to-know-about/"
    result = dc.classify_search_result(url, "These 11 Texas Restaurants Dont Need Advertising to Stay Packed")
    assert result.category == dc.ResultCategory.DISCOVERY_SOURCE


def test_hyphen_heavy_slug_alone_triggers_listicle_even_with_no_title():
    url = "https://example.com/this-tiny-texas-bbq-spot-serves-brisket-the-whole-state-talks-about/"
    result = dc.classify_search_result(url, "")
    assert result.category == dc.ResultCategory.DISCOVERY_SOURCE


def test_business_homepage_stays_official():
    result = dc.classify_search_result("https://pappadeaux.com/", "Pappadeaux Seafood Kitchen")
    assert result.category == dc.ResultCategory.OFFICIAL


def test_business_location_page_stays_official():
    result = dc.classify_search_result(
        "https://pappadeaux.com/locations/duncanville/", "Pappadeaux - Duncanville"
    )
    assert result.category == dc.ResultCategory.OFFICIAL


def test_four_hyphen_service_page_stays_official_under_threshold():
    result = dc.classify_search_result(
        "https://example.com/gluten-free-tex-mex-menu", "Gluten Free Tex Mex Menu"
    )
    assert result.category == dc.ResultCategory.OFFICIAL


def test_five_hyphen_slug_is_the_exact_threshold():
    # Exactly 5 hyphens -- the documented cutoff (">= 5").
    url = "https://example.com/one-two-three-four-five-six/"
    assert url.count("-") == 5
    result = dc.classify_search_result(url, "")
    assert result.category == dc.ResultCategory.DISCOVERY_SOURCE


def test_priceline_hotel_listing_page_is_a_discovery_source():
    """Real gap found live: priceline.com (a global hotel-booking OTA, same
    category as Expedia/Booking.com/Kayak) was missing from
    DISCOVERY_SOURCE_REGISTRY entirely, so it got deep-crawled as if it
    were a single Colorado hotel business -- following its own internal
    navigation across dozens of unrelated cities/countries (Toronto,
    Florence, Phoenix, Memphis) as "high-intent pages" of that one
    committed lead."""
    url = "https://www.priceline.com/hotel-deals/en-us/P3000033452/H65567/hilton-toronto.ssp"
    result = dc.classify_search_result(url, "Hilton Toronto - Priceline")
    assert result.category == dc.ResultCategory.DISCOVERY_SOURCE
    assert result.source_name == "Priceline"


def test_real_hotel_homepage_stays_official():
    result = dc.classify_search_result("https://www.stanleyhotel.com/", "The Stanley Hotel")
    assert result.category == dc.ResultCategory.OFFICIAL


def test_allpages_category_listing_page_is_a_discovery_source():
    """Real gap found live in the SAME run as the takemetotn.com bug:
    allpages.com (a general business directory, same category as Yellow
    Pages/Manta/Hotfrog which are already registered) was missing from
    DISCOVERY_SOURCE_REGISTRY entirely, so its category-listing page
    ("restaurants-food-dining/north-american-restaurants/texas.html") got
    committed as if the LISTING PAGE itself were a restaurant business."""
    url = "https://www.allpages.com/restaurants-food-dining/north-american-restaurants/texas.html"
    result = dc.classify_search_result(url, "North American Restaurants, Texas (TX)")
    assert result.category == dc.ResultCategory.DISCOVERY_SOURCE
    assert result.source_name == "AllPages"
