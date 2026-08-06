"""Offline tests for request validation and count/limit resolution.

No LLM or network call anywhere in this file -- both api.py's PipelineRequest
schema and phase1_pipeline.resolve_effective_limit() are pure, deterministic
logic that doesn't need a live service to test.

Run: pytest -q tests/unit
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------- #
# api.py's PipelineRequest schema
# --------------------------------------------------------------------------- #
def test_pipeline_request_accepts_a_valid_query():
    from api import PipelineRequest
    req = PipelineRequest(query="find 3 dental clinics in Islamabad")
    assert req.query == "find 3 dental clinics in Islamabad"
    assert req.concurrency == 5  # documented default


def test_pipeline_request_rejects_too_short_query():
    from api import PipelineRequest
    with pytest.raises(ValidationError):
        PipelineRequest(query="ab")  # min_length=3


def test_pipeline_request_rejects_concurrency_out_of_range():
    from api import PipelineRequest
    with pytest.raises(ValidationError):
        PipelineRequest(query="find dental clinics", concurrency=0)  # ge=1
    with pytest.raises(ValidationError):
        PipelineRequest(query="find dental clinics", concurrency=21)  # le=20


def test_pipeline_request_accepts_boundary_concurrency():
    from api import PipelineRequest
    assert PipelineRequest(query="find dental clinics", concurrency=1).concurrency == 1
    assert PipelineRequest(query="find dental clinics", concurrency=20).concurrency == 20


# --------------------------------------------------------------------------- #
# phase1_pipeline.resolve_effective_limit() -- count_explicit/default logic
# --------------------------------------------------------------------------- #
def test_effective_limit_uses_default_when_no_count_stated():
    """'find marinas in Dubai' -- no number anywhere -- must use the small
    single-pass default, not the plan's placeholder result_limit (which the
    planner is instructed to ignore the exact value of when count_explicit
    is false)."""
    import phase1_pipeline as p1
    plan = {"count_explicit": False, "result_limit": 20}
    limit, aggressive = p1.resolve_effective_limit(plan, None)
    assert limit == p1.DEFAULT_RESULT_LIMIT
    assert aggressive is False


def test_effective_limit_uses_plan_count_when_explicit():
    """'find 50 marinas in Dubai' -- count_explicit true, result_limit must
    be honored exactly, and discovery becomes aggressive (allowed to page
    Serper repeatedly to reach the count)."""
    import phase1_pipeline as p1
    plan = {"count_explicit": True, "result_limit": 50}
    limit, aggressive = p1.resolve_effective_limit(plan, None)
    assert limit == 50
    assert aggressive is True


def test_effective_limit_caller_override_wins_over_plan():
    """A caller-supplied limit (e.g. api.py's PipelineRequest doesn't have
    one today, but a future caller might) counts as an intentional
    override and always wins, and also forces aggressive discovery even if
    the query itself stated no count."""
    import phase1_pipeline as p1
    plan = {"count_explicit": False, "result_limit": 20}
    limit, aggressive = p1.resolve_effective_limit(plan, 7)
    assert limit == 7
    assert aggressive is True


def test_effective_limit_never_below_one():
    """A negative caller-supplied limit is truthy in Python (only exactly
    zero/None are falsy), so it survives the "or" fallback chain and reaches
    the max(1, ...) clamp -- must never produce a limit that would make the
    pipeline request a non-positive number of results."""
    import phase1_pipeline as p1
    plan = {"count_explicit": False, "result_limit": 20}
    limit, _ = p1.resolve_effective_limit(plan, -3)
    assert limit == 1


def test_effective_limit_zero_result_limit_falls_back_to_default():
    """A falsy-but-present result_limit (e.g. 0) does NOT survive the "or"
    chain -- it's treated the same as "no count given" and falls through to
    the default, before max(1, ...) is even relevant."""
    import phase1_pipeline as p1
    plan = {"count_explicit": True, "result_limit": 0}
    limit, _ = p1.resolve_effective_limit(plan, None)
    assert limit == p1.DEFAULT_RESULT_LIMIT
