"""Offline test for data_pipeline.to_business_level()'s homepage-selection
fix -- the real "Pier 25 Marina" bug found and fixed this session.

No network/LLM call -- pure aggregation logic over in-memory page rows.

Run: pytest -q tests/unit
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _row(**overrides):
    base = {
        "_domain": "example.org",
        "page_url": "https://example.org/",
        "website_url": "https://example.org/",
        "company_name": "Example Co",
        "email": "N/A",
        "phone_number": "N/A",
        "physical_address": "N/A",
        "page_title": "Example Co - Home",
        "meta_description": "A fictional business used for offline testing.",
        "date_added": "2026-08-05",
    }
    base.update(overrides)
    return base


def test_shared_institutional_domain_prefers_name_matching_page():
    """Reproduces the real bug: a business that's really one subsection of a
    much larger shared site (a marina's page inside a whole park authority's
    website) -- the site's true root ("/") was never crawled, only a
    generic "/contact" page (short URL, wins on path-length alone under the
    old "shortest path == homepage" heuristic) and the business's own,
    longer, actually-relevant page. page_title/description must come from
    the page whose OWN title matches the business name, not the shorter,
    generic one."""
    from data_pipeline import to_business_level

    pages = [
        _row(
            _domain="hudsonriverpark.org",
            page_url="https://hudsonriverpark.org/contact",
            website_url="https://hudsonriverpark.org/activities/pier-25-moorings/",
            company_name="Pier 25 Marina",
            page_title="Contact Us — Hudson River Park",
            meta_description="Contact Hudson River Park.",
        ),
        _row(
            _domain="hudsonriverpark.org",
            page_url="https://hudsonriverpark.org/activities/pier-25-moorings/",
            website_url="https://hudsonriverpark.org/activities/pier-25-moorings/",
            company_name="Pier 25 Marina",
            page_title="Pier 25 Marina — Hudson River Park",
            meta_description="Moorings and marina services at Pier 25.",
        ),
    ]

    businesses = to_business_level(pages)
    assert len(businesses) == 1
    biz = businesses[0]
    assert biz["company_name"] == "Pier 25 Marina"
    # The bug: this used to be "Contact Us — Hudson River Park" (the shorter,
    # generic page) instead of the marina's own page.
    assert biz["page_title"] == "Pier 25 Marina — Hudson River Park"
    assert "Moorings" in biz["description"]


def test_normal_single_page_business_unaffected():
    """The common case -- one page, root URL, page_title already matches
    the company name -- must behave exactly as before this fix."""
    from data_pipeline import to_business_level

    pages = [_row()]
    businesses = to_business_level(pages)
    assert len(businesses) == 1
    assert businesses[0]["page_title"] == "Example Co - Home"


def test_real_homepage_present_is_still_preferred():
    """When the site's actual root page WAS crawled, it must still win --
    the name-overlap fallback only activates when the shortest-path page
    ISN'T the real root ("/" or "")."""
    from data_pipeline import to_business_level

    pages = [
        _row(
            _domain="example.org",
            page_url="https://example.org/",
            company_name="Example Co",
            page_title="Example Co - Home",
        ),
        _row(
            _domain="example.org",
            page_url="https://example.org/about-something-else/",
            company_name="Example Co",
            page_title="A totally unrelated page title",
        ),
    ]
    businesses = to_business_level(pages)
    assert businesses[0]["page_title"] == "Example Co - Home"


def test_contact_union_across_pages():
    """Emails/phones found on different pages of the same business are
    unioned into one record."""
    from data_pipeline import to_business_level

    pages = [
        _row(email="info@example.org", phone_number="+15551234567"),
        _row(page_url="https://example.org/contact",
             email="N/A", phone_number="+15559876543"),
    ]
    businesses = to_business_level(pages)
    assert businesses[0]["email"] == "info@example.org"
    assert "+15551234567" in businesses[0]["phone_number"]
    assert "+15559876543" in businesses[0]["phone_number"]


def test_KNOWN_DEFECT_two_different_businesses_on_one_domain_are_merged():
    """Characterizes a REAL, CURRENTLY UNFIXED defect found live (professional
    QA suite, AI-BDM-278/279), documented in docs/KNOWN_LIMITATIONS.md --
    NOT an assertion that this is correct behavior. to_business_level()
    groups by _domain only, with no company_name component, so two
    genuinely different businesses sharing one institutional domain collapse
    into a single record: the minority business disappears entirely and its
    contact info gets unioned onto the survivor. This test pins today's
    actual (wrong) behavior so a real identity-model fix (tracked as a
    known limitation, not attempted in this pass -- see KNOWN_LIMITATIONS.md
    for why it's architecturally deeper than this one function) shows up
    here as an intentional, reviewed change rather than a silent regression
    either direction."""
    from data_pipeline import to_business_level

    pages = [
        _row(
            _domain="hudsonriverpark.org",
            page_url="https://hudsonriverpark.org/pier-25-marina/",
            company_name="Pier 25 Marina",
            page_title="Pier 25 Marina",
            email="marina@hudsonriverpark.org",
        ),
        _row(
            _domain="hudsonriverpark.org",
            page_url="https://hudsonriverpark.org/pier-30-cafe/",
            company_name="Pier 30 Cafe",
            page_title="Pier 30 Cafe",
            email="cafe@hudsonriverpark.org",
        ),
    ]
    businesses = to_business_level(pages)
    # KNOWN DEFECT: this should be 2 (two distinct businesses); it is 1.
    assert len(businesses) == 1
    assert "cafe@hudsonriverpark.org" in businesses[0]["email"]  # leaked onto the other business


def test_off_domain_email_dropped_when_own_domain_email_exists():
    """A genuinely unrelated, off-domain, non-free-webmail email (e.g. a
    mis-parsed leak from unrelated body text) is dropped in favor of the
    business's own domain when both exist. Free-webmail addresses
    (gmail.com etc.) are a separate, legitimate case -- NOT dropped, since a
    small business commonly uses one as its real contact -- confirmed
    against the real _email_is_own() logic, which treats
    ee._FREE_PROVIDERS the same as the business's own domain."""
    from data_pipeline import to_business_level

    pages = [
        _row(email="info@example.org"),
        _row(page_url="https://example.org/contact",
             email="random-leak@some-unrelated-company.biz"),
    ]
    businesses = to_business_level(pages)
    assert businesses[0]["email"] == "info@example.org"
    assert "some-unrelated-company" not in businesses[0]["email"]
