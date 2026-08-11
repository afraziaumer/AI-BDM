"""Tests for main.py's _plan_interactively() -- the interactive clarification
loop that resolves needs_clarification before Stage 2 ever runs.

Real bug found while executing a professional QA test suite
(AI-BDM-124/129, orchestration/E2E): when the user gave no answer (or the
3-round cap was hit), the old code just flipped needs_clarification to
False on the SAME summary object run_pipeline() had already returned EARLY
with -- before any discovery ever ran (the clarification gate stops the
pipeline before Stage 2, by design). "Proceeding with my best guess"
silently did nothing: it returned a summary with discovered=0, qualified=[],
claiming success while never having searched for anything. No test existed
for this function before, which is exactly how this went unnoticed.

Run: pytest -q tests/mocked/test_clarification_loop.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import main as m  # noqa: E402
import phase1_pipeline as p1  # noqa: E402


def _clarify_stub(query: str) -> dict:
    """What run_pipeline() actually returns when it stops at the
    clarification gate -- no discovery has happened yet."""
    return {
        "query": query, "plan": {"needs_clarification": True}, "discovered": 0,
        "qualified": [], "qualified_count": 0, "counts": {}, "error": None,
        "blocked": False, "needs_clarification": True,
        "clarification_questions": ["What industry?", "What location?"],
    }


def _real_result_stub(query: str) -> dict:
    """What run_pipeline() returns once discovery genuinely ran."""
    return {
        "query": query, "plan": {"needs_clarification": False}, "discovered": 3,
        "qualified": [{"company_name": "Real Co"}], "qualified_count": 1,
        "counts": {}, "error": None, "blocked": False, "needs_clarification": False,
        "clarification_questions": [],
    }


def test_no_answer_actually_runs_discovery_via_bypass(monkeypatch):
    calls = []

    async def fake_run_pipeline(query, concurrency=5, bypass_clarification=False):
        calls.append(bypass_clarification)
        if bypass_clarification:
            return _real_result_stub(query)
        return _clarify_stub(query)

    monkeypatch.setattr(p1, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")  # user presses Enter, no answer

    summary = asyncio.run(m._plan_interactively("find some businesses", 5))

    # The real bug: this used to be discovered=0/qualified=[] because the
    # bypass call never happened -- the stub from the FIRST (probe) call
    # was just relabeled instead.
    assert summary["discovered"] == 3
    assert summary["qualified_count"] == 1
    assert summary["needs_clarification"] is False
    assert True in calls  # confirms run_pipeline was actually called with bypass_clarification=True


def test_round_cap_actually_runs_discovery_via_bypass(monkeypatch):
    calls = []

    async def fake_run_pipeline(query, concurrency=5, bypass_clarification=False):
        calls.append(bypass_clarification)
        if bypass_clarification:
            return _real_result_stub(query)
        return _clarify_stub(query)

    monkeypatch.setattr(p1, "run_pipeline", fake_run_pipeline)
    # Always answers, but never satisfies the planner -- exhausts all 3 rounds.
    monkeypatch.setattr("builtins.input", lambda prompt="": "not really sure")

    summary = asyncio.run(m._plan_interactively("find some businesses", 5))

    assert summary["discovered"] == 3
    assert summary["qualified_count"] == 1
    # 3 probe calls (bypass_clarification=False) + 1 final bypass call.
    assert calls == [False, False, False, True]


def test_query_already_clear_never_triggers_the_clarification_loop(monkeypatch):
    """The common, happy-path case: needs_clarification is False on the
    very first call -- no clarification prompt, no bypass call, no
    input() prompt at all."""
    async def fake_run_pipeline(query, concurrency=5, bypass_clarification=False):
        return _real_result_stub(query)

    monkeypatch.setattr(p1, "run_pipeline", fake_run_pipeline)

    def _fail_if_called(prompt=""):
        raise AssertionError("input() should never be called when no clarification is needed")

    monkeypatch.setattr("builtins.input", _fail_if_called)

    summary = asyncio.run(m._plan_interactively("find 3 dental clinics in Islamabad", 5))
    assert summary["discovered"] == 3
    assert summary["needs_clarification"] is False
