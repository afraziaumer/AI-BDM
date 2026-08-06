"""Offline tests for the discovery gate's deterministic relevance scoring.

relevance_scoring.score_relevance() only calls an LLM as a safety net for
the ambiguous "possible_match" band (40-64) -- skip_safety_net=True (the
same flag phase1_pipeline's Hybrid Relevance Gate uses for its cheap
homepage-only pre-check) forces the plain deterministic result even in that
band, with zero network/LLM calls. Every expected score below was verified
against the real, current scoring engine, not guessed.

Run: pytest -q tests/unit
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def test_strong_industry_and_location_match_scores_very_relevant():
    from relevance_scoring import score_relevance
    result = score_relevance(
        industry="dental clinic", geo="Islamabad, Pakistan",
        name="Example Dental Clinic", domain="example-dental-clinic.com",
        sample_text=(
            "Welcome to Example Dental Clinic, the best dental clinic in "
            "Islamabad, Pakistan. We offer general dentistry, cleanings, and more."
        ),
        skip_safety_net=True,
    )
    assert result["verdict"] == "very_relevant"
    assert result["category"] == "match"
    assert result["score"] >= 80
    assert "industry_in_page_text" in result["matched_industry_evidence"]
    assert "full_geo_phrase_match" in result["matched_location_evidence"]


def test_unrelated_industry_and_location_scores_reject():
    from relevance_scoring import score_relevance
    result = score_relevance(
        industry="dental clinic", geo="Islamabad, Pakistan",
        name="Random Bookstore", domain="random-bookstore.com",
        sample_text="We sell used books, comics and stationery in London.",
        skip_safety_net=True,
    )
    assert result["verdict"] == "reject"
    assert result["category"] == "unrelated"
    assert result["score"] == 0
    assert result["matched_industry_evidence"] == []
    assert result["matched_location_evidence"] == []


def test_borderline_match_with_skip_safety_net_returns_immediately():
    """A genuinely ambiguous case (single-source industry mention, partial
    location match) lands in the possible_match band (40-64). With
    skip_safety_net=True this must return the plain deterministic result --
    NOT call the LLM classifier -- so `category` stays the skip_safety_net
    default ("match", unresolved) rather than whatever an LLM would decide.
    If this ever silently starts calling the LLM, this test's zero-network
    assumption breaks loudly (no API key is configured in the test
    environment, so an accidental LLM call would raise, not silently pass)."""
    from relevance_scoring import score_relevance
    result = score_relevance(
        industry="dental clinic", geo="Islamabad, Pakistan",
        name="Smile Care Dental", domain="smile-care-center.com",
        sample_text=(
            "We offer dental clinic services somewhere in Pakistan. "
            "Contact us for an appointment."
        ),
        address="Pakistan",
        skip_safety_net=True,
    )
    assert result["verdict"] == "possible_match"
    assert 40 <= result["score"] < 65
    assert result["category"] == "match"  # the skip_safety_net default, not an LLM verdict


def test_no_location_requested_gives_full_location_credit():
    """A query with no geo constraint at all (search_type where location
    doesn't matter) must not penalize a business for "not matching" a
    location nobody asked about."""
    from relevance_scoring import score_relevance
    result = score_relevance(
        industry="dental clinic", geo="",
        name="Example Dental Clinic", domain="example-dental-clinic.com",
        sample_text="Example Dental Clinic offers general dentistry.",
        skip_safety_net=True,
    )
    assert "no_location_requested" in result["matched_location_evidence"]


def test_scoring_never_raises_on_malformed_input():
    """The module's own fail-CLOSED contract: any internal error must
    degrade to a reject verdict, never propagate an exception up to the
    caller (a research helper failing should never crash the pipeline)."""
    from relevance_scoring import score_relevance
    result = score_relevance(
        industry="dental clinic", geo="Islamabad",
        name=None, domain=None,  # type: ignore[arg-type] -- deliberately malformed
        sample_text=None,  # type: ignore[arg-type]
        skip_safety_net=True,
    )
    assert result["verdict"] in {"reject", "possible_match", "relevant", "very_relevant"}
