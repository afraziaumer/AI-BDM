"""Tests for phase3/google_maps.py's identity-verification hierarchy:
address match -> phone match -> name-only fallback (single candidate only).
The pure matching helpers need no mocking at all; find_place()'s
orchestration is tested with a mocked Serper Places call.

Run: pytest -q tests/mocked
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------- #
# Pure helpers -- no mocking needed
# --------------------------------------------------------------------------- #
def test_address_match_requires_a_distinctive_shared_token():
    from phase3.google_maps import _looks_like_match
    assert _looks_like_match(
        "H-12 Street 5, Blue Area, Islamabad", "Office 3, Blue Area, Islamabad"
    ) is True  # "blue"/"area" shared, distinctive


def test_address_match_rejects_shared_city_alone():
    """Two different businesses in the same city sharing only the city name
    is NOT evidence they're the same place -- this is the exact false-
    positive class this function exists to prevent."""
    from phase3.google_maps import _looks_like_match
    assert _looks_like_match(
        "Random Street, Islamabad, Pakistan", "Completely Different Road, Islamabad, Pakistan"
    ) is False


def test_address_match_false_when_no_known_address():
    """No address on file means no verification is possible -- must return
    False (can't confirm), never guess a match."""
    from phase3.google_maps import _looks_like_match
    assert _looks_like_match("Blue Area, Islamabad", "") is False


def test_phone_match_tolerant_of_formatting():
    from phase3.google_maps import _phone_matches
    assert _phone_matches("+92 51 234 5678", "0512345678") is True
    assert _phone_matches("+92 51 234 5678", "051-234-5678 | +1 555 000 1111") is True


def test_phone_match_rejects_different_numbers():
    from phase3.google_maps import _phone_matches
    assert _phone_matches("+92 51 234 5678", "+1 555 000 1111") is False


def test_phone_match_false_when_no_known_phone():
    from phase3.google_maps import _phone_matches
    assert _phone_matches("+92 51 234 5678", "") is False


def test_name_only_requires_full_containment():
    from phase3.google_maps import _name_only_match
    assert _name_only_match("Example Dental Clinic Islamabad", "Example Dental Clinic") is True
    assert _name_only_match("Example Clinic", "Example Dental Clinic") is False  # missing "dental"


# --------------------------------------------------------------------------- #
# find_place() -- the full tier-fallback orchestration, Serper mocked
# --------------------------------------------------------------------------- #
def _place(title="", address="", phone="", cid="cid123", rating=4.5, rating_count=10):
    return {
        "title": title, "address": address, "phoneNumber": phone,
        "cid": cid, "rating": rating, "ratingCount": rating_count,
    }


def _run_find_place(monkeypatch, places, name="Example Co", address="", geo="", phone=""):
    import phase3.google_maps as gm

    async def fake_serper_places(session, query, num=5):
        return places

    monkeypatch.setattr(gm, "_serper_places", fake_serper_places)
    return asyncio.run(gm.find_place(session=None, name=name, address=address, geo=geo, phone=phone))


def test_address_match_wins_when_available(monkeypatch):
    places = [_place(title="Example Co", address="Blue Area, Islamabad")]
    result = _run_find_place(
        monkeypatch, places, name="Example Co", address="Blue Area, Islamabad",
    )
    assert result["matched"] is True
    assert result["match_basis"] == "address"


def test_phone_match_used_when_address_unavailable(monkeypatch):
    places = [_place(title="Example Co", address="", phone="+92 51 234 5678")]
    result = _run_find_place(
        monkeypatch, places, name="Example Co", address="", phone="0512345678",
    )
    assert result["matched"] is True
    assert result["match_basis"] == "phone"


def test_name_only_fallback_for_single_candidate_with_no_address_or_phone(monkeypatch):
    places = [_place(title="Example Co Islamabad")]
    result = _run_find_place(
        monkeypatch, places, name="Example Co", address="", phone="",
    )
    assert result["matched"] is True
    assert result["match_basis"] == "name_only"
    assert result["confidence"] == "low"  # name-only is explicitly lower confidence


def test_name_only_fallback_disabled_with_multiple_candidates(monkeypatch):
    """The exact real risk this guards against: several similarly-named
    chain locations returned, none with an address/phone to verify --
    picking one by name alone would have real odds of choosing the wrong
    location."""
    places = [_place(title="Example Co Islamabad", cid="a"), _place(title="Example Co Lahore", cid="b")]
    result = _run_find_place(
        monkeypatch, places, name="Example Co", address="", phone="",
    )
    assert result["matched"] is False


def test_extract_fields_treats_explicit_zero_rating_count_as_valid():
    """Real bug found live (professional QA suite, AI-BDM-196 "zero reviews
    -> 0 accepted as valid, not negative"): the old `or`-chain
    (place.get("ratingCount") or place.get("reviewsCount") or ...) falls
    through on ANY falsy value, so a place with a genuine ratingCount=0 got
    silently overridden by whatever the NEXT key in the chain held instead
    of the explicit 0 being respected."""
    from phase3.google_maps import _extract_fields
    place = {"title": "X", "address": "", "rating": 4.5,
             "ratingCount": 0, "reviewsCount": 15, "phoneNumber": ""}
    assert _extract_fields(place)["rating_count"] == 0


def test_extract_fields_falls_back_to_the_next_key_when_the_first_is_absent():
    from phase3.google_maps import _extract_fields
    place = {"title": "Y", "address": "", "reviewsCount": 8, "phoneNumber": ""}
    assert _extract_fields(place)["rating_count"] == 8


def test_extract_fields_defaults_to_zero_when_no_count_key_is_present():
    from phase3.google_maps import _extract_fields
    place = {"title": "Z", "address": "", "phoneNumber": ""}
    assert _extract_fields(place)["rating_count"] == 0


def test_no_results_returns_unmatched(monkeypatch):
    result = _run_find_place(monkeypatch, [], name="Example Co")
    assert result["matched"] is False
    assert result["reason"] == "no Serper results"


def test_shared_address_disambiguated_by_phone(monkeypatch):
    """Real bug found live (professional QA suite, DATA-012 "two businesses
    same address"): two unrelated businesses share a building, both share a
    token with the known address. The OLD code returned whichever candidate
    Serper happened to list first, even when a later candidate's phone
    number actually matched -- silently attaching a competitor's rating/
    category/reviews to the wrong business. Phone must disambiguate."""
    places = [
        _place(title="Jones Dental", address="123 Main St, Plaza", phone="555-2222", cid="wrong"),
        _place(title="Smith Dental", address="123 Main St, Plaza", phone="555-1111", cid="right"),
    ]
    result = _run_find_place(
        monkeypatch, places, name="Smith Dental",
        address="123 Main St, Plaza", phone="555-1111",
    )
    assert result["matched"] is True
    assert result["cid"] == "right"
    assert result["match_basis"] == "address+corroborated"


def test_shared_address_with_no_corroboration_does_not_guess(monkeypatch):
    """Same shared-address scenario, but this time neither candidate's phone
    (nor a full name match) confirms which one is right -- must NOT guess
    by picking the first one, same 'never guess' principle as every other
    tier in this hierarchy."""
    places = [
        _place(title="Jones Dental", address="123 Main St, Plaza", phone="555-2222", cid="a"),
        _place(title="Other Dental", address="123 Main St, Plaza", phone="555-3333", cid="b"),
    ]
    result = _run_find_place(
        monkeypatch, places, name="Smith Dental Group",
        address="123 Main St, Plaza", phone="555-1111",
    )
    assert result["matched"] is False
