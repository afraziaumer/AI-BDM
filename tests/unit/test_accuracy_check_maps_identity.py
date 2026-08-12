"""Tests for accuracy_check.check_identity_consistency()'s Google Maps
cross-check -- a real gap found while executing the professional QA suite's
new Data Consistency section (DATA-002/DATA-004): google_maps.json is
deliberately excluded from _iter_review_records (it's keyed "title", not
"business_name" -- see that function's own docstring), which meant a Maps
listing matched via ADDRESS or PHONE (not name) never had its name checked
against company_name at all, and the matched Maps address was never
cross-checked against the website's own scraped address either. A business
matched by address/phone can legitimately have a very different name (a
rebrand, a chain listing, a nearby business sharing an address) and nothing
flagged it before this fix.

Run: pytest -q tests/unit/test_accuracy_check_maps_identity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import accuracy_check as ac  # noqa: E402

DOMAIN = "does-not-exist-on-disk.example"  # no reviews/ dir -> _iter_review_records yields nothing


def test_no_cached_maps_record_is_a_safe_no_op(monkeypatch):
    monkeypatch.setattr(ac.store, "load", lambda domain, platform: None)
    flags = ac.check_identity_consistency(DOMAIN, "Acme Dental", "Acme Dental | Home", "123 Main St")
    assert flags == []


def test_unmatched_maps_record_is_ignored(monkeypatch):
    """matched=False means google_maps.py itself couldn't verify the listing
    -- nothing here to compare it against, so no flag (same 'never guess'
    principle as the matching module itself)."""
    monkeypatch.setattr(
        ac.store, "load",
        lambda domain, platform: {"matched": False, "title": "Totally Different Co"},
    )
    flags = ac.check_identity_consistency(DOMAIN, "Acme Dental", "Acme Dental", "123 Main St")
    assert flags == []


def test_matching_maps_name_produces_no_flag(monkeypatch):
    monkeypatch.setattr(
        ac.store, "load",
        lambda domain, platform: {
            "matched": True, "match_basis": "address",
            "title": "Acme Dental Clinic", "address": "123 Main St, Springfield",
        },
    )
    flags = ac.check_identity_consistency(
        DOMAIN, "Acme Dental Clinic", "Acme Dental Clinic | Home", "123 Main St, Springfield",
    )
    assert flags == []


def test_mismatched_maps_name_is_flagged():
    """The real gap: matched via address/phone, but the Maps listing's own
    name barely overlaps company_name -- this used to pass through silently
    because google_maps.json was skipped by _iter_review_records entirely."""
    import unittest.mock as mock
    with mock.patch.object(ac.store, "load", return_value={
        "matched": True, "match_basis": "phone",
        "title": "Springfield Family Practice", "address": "123 Main St",
    }):
        flags = ac.check_identity_consistency(DOMAIN, "Acme Dental Clinic", "Acme Dental Clinic", "123 Main St")
    assert any("google_maps listing name barely matches" in f for f in flags)
    assert any("matched via phone" in f for f in flags)


def test_mismatched_maps_address_is_flagged():
    import unittest.mock as mock
    with mock.patch.object(ac.store, "load", return_value={
        "matched": True, "match_basis": "phone",
        "title": "Acme Dental Clinic", "address": "999 Totally Different Rd, Other City",
    }):
        flags = ac.check_identity_consistency(
            DOMAIN, "Acme Dental Clinic", "Acme Dental Clinic", "123 Main St, Springfield",
        )
    assert any("google_maps listing address shares no distinctive word" in f for f in flags)


def test_missing_physical_address_does_not_produce_an_address_flag():
    """No known address means no verification is possible -- same
    never-guess rule the rest of this module already follows; must not
    flag just because our own side of the comparison is blank."""
    import unittest.mock as mock
    with mock.patch.object(ac.store, "load", return_value={
        "matched": True, "match_basis": "phone",
        "title": "Acme Dental Clinic", "address": "999 Totally Different Rd",
    }):
        flags = ac.check_identity_consistency(DOMAIN, "Acme Dental Clinic", "Acme Dental Clinic", "")
    assert flags == []


def test_name_only_match_basis_never_flags_name_by_construction():
    """match_basis='name_only' requires full name-token containment to have
    matched at all (see google_maps._name_only_match) -- overlap can never
    fall below NAME_OVERLAP_MIN for that path, so this must never produce a
    spurious flag."""
    import unittest.mock as mock
    with mock.patch.object(ac.store, "load", return_value={
        "matched": True, "match_basis": "name_only", "confidence": "low",
        "title": "Acme Dental Clinic Springfield", "address": "",
    }):
        flags = ac.check_identity_consistency(DOMAIN, "Acme Dental Clinic", "Acme Dental Clinic", "")
    assert flags == []
