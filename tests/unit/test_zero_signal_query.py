"""Tests for main._is_zero_signal_plan().

Real gap found live: "basit goes to work" (a nonsensical, non-lead-gen
query -- industry/location/search_query all came back blank from the
planner) got the exact same "a bit more detail would help before I
search" framing as a genuine but under-specified request like "find some
dental clinics" (which DOES have an industry, just no location). Both set
needs_clarification=True, but they aren't the same situation -- one just
needs a detail filled in, the other isn't a business search at all. This
distinguishes the two so the message shown can be honest.

Run: pytest -q tests/unit/test_zero_signal_query.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import main as m  # noqa: E402


def test_completely_blank_plan_is_zero_signal():
    plan = {"broad_industry": "", "geo_location": "", "search_query": ""}
    assert m._is_zero_signal_plan(plan) is True


def test_missing_keys_entirely_is_also_zero_signal():
    assert m._is_zero_signal_plan({}) is True


def test_industry_only_is_not_zero_signal():
    plan = {"broad_industry": "dental clinic", "geo_location": "", "search_query": ""}
    assert m._is_zero_signal_plan(plan) is False


def test_location_only_is_not_zero_signal():
    plan = {"broad_industry": "", "geo_location": "Austin, TX", "search_query": ""}
    assert m._is_zero_signal_plan(plan) is False


def test_search_query_only_is_not_zero_signal():
    plan = {"broad_industry": "", "geo_location": "", "search_query": "dental clinics"}
    assert m._is_zero_signal_plan(plan) is False


def test_fully_specified_plan_is_not_zero_signal():
    plan = {
        "broad_industry": "dental clinic", "geo_location": "Islamabad, Pakistan",
        "search_query": "dental clinics in Islamabad",
    }
    assert m._is_zero_signal_plan(plan) is False


def test_whitespace_only_fields_still_count_as_zero_signal():
    plan = {"broad_industry": "   ", "geo_location": "\n", "search_query": ""}
    assert m._is_zero_signal_plan(plan) is True
