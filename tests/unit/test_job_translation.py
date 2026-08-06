"""Offline tests for job_translation.target_to_query() -- the structured
JobRequest -> natural-language query bridge, and specifically that every
field it can't honestly honor produces an explicit warning rather than a
silent drop.

No LLM or network call -- pure string construction, no calls into
LLM_planner itself.

Run: pytest -q tests/unit/test_job_translation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import job_contracts as jc  # noqa: E402
from job_translation import target_to_query  # noqa: E402


def _minimal_request(**overrides) -> jc.JobRequest:
    base = dict(
        job_id="job_01", tenant_ref="org_01", correlation_id="cor_01",
        requested_at=jc.utc_now(),
        target=jc.Target(industry="dental clinics"),
        limits=jc.Limits(max_prospects=5),
        features=jc.Features(maps=False, reviews=True, tech_stack=False),
    )
    base.update(overrides)
    return jc.JobRequest(**base)


def test_basic_industry_and_count_with_no_location():
    req = _minimal_request()
    query, warnings = target_to_query(req)
    assert query == "find 5 dental clinics businesses"
    assert warnings == []


def test_single_location_included_in_query():
    req = _minimal_request(
        target=jc.Target(
            industry="marinas",
            locations=[jc.Location(country="US", region="FL", city="Miami")],
        )
    )
    query, warnings = target_to_query(req)
    assert query == "find 5 marinas businesses in Miami, FL, US"
    assert warnings == []


def test_multiple_locations_joined_and_warned():
    req = _minimal_request(
        target=jc.Target(
            industry="marinas",
            locations=[
                jc.Location(country="US", city="Miami"),
                jc.Location(country="US", city="Tampa"),
            ],
        )
    )
    query, warnings = target_to_query(req)
    assert "Miami, US or Tampa, US" in query
    assert len(warnings) == 1
    assert "2 entries" in warnings[0]


def test_company_size_dropped_with_explicit_warning():
    req = _minimal_request(
        target=jc.Target(
            industry="marinas",
            company_size=jc.CompanySize(min_employees=10, max_employees=250),
        )
    )
    query, warnings = target_to_query(req)
    # Never silently reflected in the query string as if it were honored.
    assert "employee" not in query.lower()
    assert any("company_size" in w for w in warnings)


def test_tech_stack_feature_adds_hint_and_warning():
    req = _minimal_request(features=jc.Features(maps=False, reviews=True, tech_stack=True))
    query, warnings = target_to_query(req)
    assert "technology stack" in query
    assert any("features.tech_stack" in w for w in warnings)


def test_maps_true_warns_because_stage_7_is_out_of_scope():
    req = _minimal_request(features=jc.Features(maps=True, reviews=True, tech_stack=False))
    _, warnings = target_to_query(req)
    assert any("features.maps=true" in w for w in warnings)


def test_maps_false_is_silently_fine_no_warning():
    req = _minimal_request(features=jc.Features(maps=False, reviews=True, tech_stack=False))
    _, warnings = target_to_query(req)
    assert not any("maps" in w for w in warnings)


def test_reviews_false_warns_not_yet_honored_per_job():
    req = _minimal_request(features=jc.Features(maps=False, reviews=False, tech_stack=False))
    _, warnings = target_to_query(req)
    assert any("features.reviews=false" in w for w in warnings)


def test_no_warnings_for_a_fully_honorable_request():
    req = _minimal_request(
        target=jc.Target(industry="dental clinics", locations=[jc.Location(country="PK", city="Islamabad")]),
        features=jc.Features(maps=False, reviews=True, tech_stack=False),
    )
    _, warnings = target_to_query(req)
    assert warnings == []
