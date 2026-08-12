"""Offline smoke tests -- no network calls, no API keys required.

Proves: (1) the core modules import cleanly (catches the class of bug this
project hit twice this session -- a native-library segfault and a
third-party package encoding crash, both invisible until actually
imported/run), and (2) a real, non-trivial, genuinely offline-capable slice
of the pipeline (Stage 8's accuracy checks) produces correct structured
output against fixture data.

Run: pytest -q tests/smoke
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------- #
# 1. Import smoke test
# --------------------------------------------------------------------------- #
CORE_MODULES = [
    "domain_utils",
    "storage",
    "accuracy_check",
    "data_pipeline",
    "model_router",
    "LLM_planner",
]


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_core_module_imports(module_name):
    """Each core module must import without raising -- this is exactly the
    kind of failure that was previously invisible: a Windows native-library
    segfault (pyarrow/datasets, see README's post-install step) and a
    third-party package UTF-8 encoding crash (wappalyzer, see
    scripts/patch_wappalyzer.py) both only surfaced at import/use time, with
    no signal from a syntax check alone."""
    __import__(module_name)


def test_full_pipeline_module_imports():
    """phase1_pipeline.py is the largest, most dependency-heavy module in the
    project (imports nearly every other first-party module transitively) --
    a clean import of this one module is a strong signal the whole
    dependency graph installed correctly."""
    import phase1_pipeline  # noqa: F401


# --------------------------------------------------------------------------- #
# 2. Accuracy audit (Stage 8) against fixture data -- genuinely offline,
#    no LLM/network call anywhere in this code path.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fixture_row():
    csv_path = FIXTURES / "leads_with_maps_sample.csv"
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    return rows[0]


@pytest.fixture(autouse=True)
def _point_accuracy_check_at_fixtures(monkeypatch):
    """accuracy_check.py resolves storage/<domain>/reviews/ off module-level
    STORAGE_ROOT/REVIEWS_SUBDIR constants -- repoint them at the fixture
    directory for the duration of each test rather than touching the real
    storage/ folder."""
    import accuracy_check
    monkeypatch.setattr(accuracy_check, "STORAGE_ROOT", str(FIXTURES / "fixture_storage"))


def test_field_validity_passes_clean_fixture(fixture_row):
    import accuracy_check
    flags = accuracy_check.check_field_validity(fixture_row)
    assert flags == [], f"expected no field-validity flags on clean fixture data, got: {flags}"


def test_field_validity_catches_bad_email(fixture_row):
    import accuracy_check
    bad_row = dict(fixture_row)
    bad_row["email"] = "not-an-email-address"
    flags = accuracy_check.check_field_validity(bad_row)
    assert any("email" in f for f in flags)


def test_field_validity_catches_out_of_range_rating(fixture_row):
    import accuracy_check
    bad_row = dict(fixture_row)
    bad_row["maps_rating"] = "7.5"  # ratings are 0-5
    flags = accuracy_check.check_field_validity(bad_row)
    assert any("maps_rating" in f for f in flags)


def test_field_validity_catches_malformed_phone(fixture_row):
    import accuracy_check
    bad_row = dict(fixture_row)
    bad_row["phone_number"] = "123"
    flags = accuracy_check.check_field_validity(bad_row)
    assert any("phone" in f for f in flags)


def test_field_validity_accepts_a_valid_phone(fixture_row):
    import accuracy_check
    row = dict(fixture_row)
    row["phone_number"] = "+1 555 234 5678"
    flags = accuracy_check.check_field_validity(row)
    assert not any("phone" in f for f in flags)


def test_field_validity_catches_negative_review_count(fixture_row):
    import accuracy_check
    bad_row = dict(fixture_row)
    bad_row["maps_rating_count"] = "-5"
    flags = accuracy_check.check_field_validity(bad_row)
    assert any("maps_rating_count" in f for f in flags)


def test_field_validity_accepts_zero_review_count_as_valid(fixture_row):
    """Zero reviews is a genuine, valid state -- must not be flagged as if
    it were negative/invalid data."""
    import accuracy_check
    row = dict(fixture_row)
    row["maps_rating_count"] = "0"
    flags = accuracy_check.check_field_validity(row)
    assert not any("maps_rating_count" in f for f in flags)


def test_identity_consistency_passes_matching_fixture(fixture_row):
    """The fixture's company_name ("Example Dental Clinic") and page_title
    ("Example Dental Clinic - Home") share real word overlap, and the
    google_reviews fixture's business_name matches exactly -- none of this
    should be flagged."""
    import accuracy_check
    flags = accuracy_check.check_identity_consistency(
        domain=fixture_row["domain"],
        company_name=fixture_row["company_name"],
        page_title=fixture_row["page_title"],
    )
    assert flags == [], f"expected no identity-consistency flags, got: {flags}"


def test_identity_consistency_flags_real_mismatch(fixture_row):
    """A page_title sharing no words with the company name must be flagged
    -- this is the exact class of bug (a false-positive business match)
    this check exists to catch."""
    import accuracy_check
    flags = accuracy_check.check_identity_consistency(
        domain=fixture_row["domain"],
        company_name=fixture_row["company_name"],
        page_title="Completely Unrelated Page Title About Something Else",
    )
    assert any("barely matches" in f for f in flags)


def test_review_relevance_flags_only_the_offtopic_mention(fixture_row):
    """The fixture's reddit.json has one review that names the business
    directly and one generic, unrelated reply -- exactly one flag is
    expected, not zero and not both."""
    import accuracy_check
    flags = accuracy_check.check_review_relevance(
        domain=fixture_row["domain"],
        company_name=fixture_row["company_name"],
        geo="Islamabad",
    )
    assert len(flags) == 1, f"expected exactly 1 relevance flag, got {len(flags)}: {flags}"
    assert "waterflosser" in flags[0]


def test_accuracy_run_produces_a_report(tmp_path, fixture_row, monkeypatch):
    """End-to-end offline run of accuracy_check.run() against the fixture
    CSV, proving the whole Stage 8 entry point works, not just its three
    check functions in isolation."""
    import accuracy_check

    report_path = tmp_path / "accuracy_report.txt"
    monkeypatch.setattr(accuracy_check, "REPORT_PATH", str(report_path))

    accuracy_check.run(geo="Islamabad", path=str(FIXTURES / "leads_with_maps_sample.csv"))

    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "Businesses audited: 1" in content
