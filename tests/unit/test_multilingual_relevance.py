"""Tests for accuracy_check._is_english()'s role in check_review_relevance():
non-Latin-script reviews (Arabic/Chinese/Cyrillic) must not be flagged as
"unrelated chatter" solely because word-overlap can't judge relevance in a
language it doesn't read. Live-verified 2026-08-12 (professional QA suite,
REG-012) against the real langdetect model, not a synthetic stub.

Run: pytest -q tests/unit/test_multilingual_relevance.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import accuracy_check as ac  # noqa: E402


def test_arabic_text_is_not_detected_as_english():
    assert ac._is_english("هذا المطعم رائع جدا وأنصح بزيارته") is False


def test_chinese_text_is_not_detected_as_english():
    assert ac._is_english("这家餐厅非常好，我强烈推荐") is False


def test_cyrillic_text_is_not_detected_as_english():
    assert ac._is_english("Этот ресторан очень хороший, я рекомендую") is False


def test_genuine_english_text_is_detected_as_english():
    assert ac._is_english("This restaurant is really great and I recommend it") is True


def test_non_english_reddit_review_with_no_name_or_geo_overlap_is_not_flagged(tmp_path, monkeypatch):
    """The actual failure mode this guards against: a Reddit mention in
    Arabic that shares no token with the business name or geo would, under
    pure word-overlap logic, look exactly like "unrelated chatter" -- unless
    the language gate exempts it first."""
    domain = "example-multilingual-test.com"
    reviews_dir = tmp_path / domain / "reviews"
    reviews_dir.mkdir(parents=True)
    (reviews_dir / "reddit.json").write_text(
        json.dumps({
            "platform": "reddit",
            "reviews": [{"text": "هذا المطعم رائع جدا وأنصح بزيارته"}],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(ac, "STORAGE_ROOT", str(tmp_path))
    flags = ac.check_review_relevance(domain, company_name="Totally Different Name Co", geo="Some City")
    assert flags == []


def test_listing_based_platform_is_never_re_litigated_by_relevance_check(tmp_path, monkeypatch):
    """REG-013: check_review_relevance() must only apply its word-overlap
    heuristic to search-based platforms (currently just reddit). A
    business-confirmed LISTING platform (google_reviews, yelp, ...) has
    already been identity-verified by check_identity_consistency() -- an
    individual review with off-topic text there must not be flagged just
    because it doesn't happen to mention the business name or geo."""
    domain = "listing-platform-test.com"
    reviews_dir = tmp_path / domain / "reviews"
    reviews_dir.mkdir(parents=True)
    (reviews_dir / "google_reviews.json").write_text(
        json.dumps({
            "platform": "google_reviews", "matched": True, "business_name": "Acme Dental",
            "reviews": [{"text": "The weather was nice today and I bought some groceries."}],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(ac, "STORAGE_ROOT", str(tmp_path))
    flags = ac.check_review_relevance(domain, company_name="Acme Dental", geo="Springfield")
    assert flags == []
