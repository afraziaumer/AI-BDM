"""Tests for phase1_pipeline._strip_clarification_history().

Real defect found live: main.py's _plan_interactively() appends
"(Previously asked: ... -- answer: ...)" blocks onto the query so a fresh
LLM re-planning call has the context it needs to resolve an answer
correctly -- that's legitimate and still happens. But the SAME
history-appended string was then stored verbatim as summary["query"] and
in last_run.json, which rag/top_matches.py's focus-concept extraction
also reads as the literal search intent. Words like "previously" and
"asked" became part of the significant-word pool used to pick the
ranking's focus_word, and could outrank the real concept (e.g.
"reservation") -- weakening the exact mechanism meant to surface
concept-relevant chunks to the top of the results.

Run: pytest -q tests/unit/test_clarification_history_stripping.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import phase1_pipeline as p1  # noqa: E402


def test_strips_a_single_clarification_round():
    polluted = (
        'find 3 dental clinics in Islamabad\n\n'
        '(Previously asked: "chain or independent?" -- answer: "both")'
    )
    assert p1._strip_clarification_history(polluted) == "find 3 dental clinics in Islamabad"


def test_strips_multiple_clarification_rounds():
    polluted = (
        "find 10 restaurants in Texas with no online reservation system\n\n"
        '(Previously asked: "Are you looking for individual restaurant locations '
        'only, or should we count each location of a multi-location chain as a '
        'separate lead?" -- answer: "both work fine")\n\n'
        '(Previously asked: "Do you have a specific city or region in Texas you '
        'want to target, or should we search the entire state?" -- answer: '
        '"search entire state")'
    )
    assert p1._strip_clarification_history(polluted) == (
        "find 10 restaurants in Texas with no online reservation system"
    )


def test_query_with_no_clarification_history_is_unchanged():
    clean = "find 3 dental clinics in Islamabad"
    assert p1._strip_clarification_history(clean) == clean


def test_run_pipeline_summary_query_is_cleaned_but_planner_gets_full_history(monkeypatch):
    """The two halves of the fix, verified together: the actual LLM
    planning call still receives the FULL history-appended query (needed
    to resolve the clarification answer), but summary["query"] -- what
    gets shown to the user and saved to last_run.json for RAG -- is clean."""
    import asyncio

    polluted = (
        'find 3 dental clinics in Islamabad\n\n'
        '(Previously asked: "chain or independent?" -- answer: "both")'
    )
    received_by_planner = {}

    async def fake_deconstruct_intent(user_query):
        received_by_planner["query"] = user_query
        return {
            "needs_clarification": False, "clarification_questions": [],
            "geo_location": "Islamabad", "broad_industry": "dental clinic",
            "search_query": "dental clinics in Islamabad", "result_limit": 3,
            "count_explicit": True, "search_type": "general", "target_domain": "",
            "intent": "find", "country_code": "PK", "phone_regex": "",
            "needs_tech_stack": False, "exclude_keywords": [], "include_keywords": [],
        }

    monkeypatch.setattr(p1, "deconstruct_intent", fake_deconstruct_intent)
    monkeypatch.setattr(p1, "moderate_query", lambda q: {"safe": True, "category": "", "reason": ""})
    # No Serper key -> discovery's own documented early-return path
    # ("Serper key missing... return [], start_page") fires immediately, so
    # this test doesn't need to know discovery's internal function names.
    monkeypatch.setattr(p1, "SERPER_API_KEY", "")

    summary = asyncio.run(p1.run_pipeline(polluted, concurrency=2))

    assert received_by_planner["query"] == polluted  # planner got the FULL context
    assert summary["query"] == "find 3 dental clinics in Islamabad"  # but summary is clean
