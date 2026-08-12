"""Tests for rag/top_matches.py's _negation_flag() -- distinguishing a
negative ("doesn't have CRM"), future ("CRM planned next year"), and
positive/current ("CRM currently used") state for the same matched word.
Previously untested in isolation.

Run: pytest -q tests/unit/test_top_matches_negation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from rag.top_matches import _negation_flag  # noqa: E402


# --------------------------------------------------------------------------- #
# AI-BDM-177 / REG-*: hard negation
# --------------------------------------------------------------------------- #
def test_no_x_is_negated():
    assert _negation_flag("this business has no online booking system", "booking") is True


def test_doesnt_have_x_is_negated():
    assert _negation_flag("the clinic doesn't have a crm", "crm") is True


def test_without_x_is_negated():
    assert _negation_flag("they operate without a crm", "crm") is True


def test_lacking_x_is_negated():
    assert _negation_flag("the site is lacking analytics", "analytics") is True


# --------------------------------------------------------------------------- #
# AI-BDM-178: future-state ("planned next year") must not read as current
# --------------------------------------------------------------------------- #
def test_coming_soon_is_treated_as_not_yet_present():
    assert _negation_flag("our new crm is coming soon", "crm") is True


def test_in_progress_is_treated_as_not_yet_present():
    assert _negation_flag("the booking system is in progress", "booking") is True


def test_under_development_is_treated_as_not_yet_present():
    assert _negation_flag("a client portal is under development", "portal") is True


def test_planned_for_next_year_is_treated_as_not_yet_present():
    assert _negation_flag("crm integration is planned for next year", "crm") is True


def test_not_yet_available_is_treated_as_not_yet_present():
    assert _negation_flag("online booking is not yet available", "booking") is True


# --------------------------------------------------------------------------- #
# AI-BDM-179: genuine positive/current state must NOT be flagged as negated
# --------------------------------------------------------------------------- #
def test_currently_used_is_not_negated():
    assert _negation_flag("the clinic currently uses a crm", "crm") is False


def test_plain_positive_statement_is_not_negated():
    assert _negation_flag("we launched our new booking app last month", "app") is False


def test_word_not_present_returns_none():
    assert _negation_flag("this text says nothing relevant", "crm") is None


# --------------------------------------------------------------------------- #
# Edge cases the docstring explicitly calls out
# --------------------------------------------------------------------------- #
def test_negation_meant_for_a_later_word_does_not_leak_backward():
    """The exact case the module's own docstring warns about: a "no" meant
    for a LATER word must not be misread as negating an EARLIER word in the
    same short text."""
    assert _negation_flag("marina with no app", "marina") is False


def test_coming_up_this_weekend_is_not_a_false_future_state_match():
    """"coming" alone isn't the future-state signal -- it requires an actual
    time-reference tail ("coming soon/next X/this X/in <year>"). "coming up
    this weekend" is a real, unrelated use of "coming" (an event schedule,
    not a not-yet-built feature) and must not be flagged."""
    assert _negation_flag("events coming up this weekend at the marina", "marina") is False
