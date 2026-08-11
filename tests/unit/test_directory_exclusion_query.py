"""Tests for phase1_pipeline._append_directory_exclusions() -- the
"never even see these in search results" fix requested directly: instead
of discovering a known OTA/directory, fetching it, classifying it, and
THEN rejecting it, exclude it from the Google/Serper query itself so it
never appears as a result at all.

Run: pytest -q tests/unit/test_directory_exclusion_query.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import learned_directories as ld  # noqa: E402
import phase1_pipeline as p1  # noqa: E402


def _isolated_file(tmp_path, monkeypatch):
    path = str(tmp_path / "test_learned_directories.json")
    monkeypatch.setattr(ld, "LEARNED_DIRECTORIES_FILE", path)
    return path


def test_original_query_text_is_preserved_at_the_front():
    result = p1._append_directory_exclusions("boutique hotels in Colorado")
    assert result.startswith("boutique hotels in Colorado ")


def test_known_registry_brands_get_dot_com_appended():
    result = p1._append_directory_exclusions("hotels in Toronto")
    assert "-site:booking.com" in result
    assert "-site:priceline.com" in result
    assert "-site:expedia.com" in result


def test_learned_domain_is_used_verbatim_not_dot_com_appended(tmp_path, monkeypatch):
    """Real formatting bug caught before it shipped: registry keys are
    bare brand names needing ".com" appended, but learned domains are
    already complete (including non-.com TLDs, e.g. a real "expedia.ca"
    seen live) -- blindly appending ".com" to both would have produced
    "expedia.ca.com"."""
    _isolated_file(tmp_path, monkeypatch)
    ld.record_learned_directory("expedia.ca", reason="test")
    result = p1._append_directory_exclusions("hotels in Toronto")
    assert "-site:expedia.ca" in result
    assert "-site:expedia.ca.com" not in result


def test_newly_learned_unknown_domain_appears_in_the_next_query(tmp_path, monkeypatch):
    _isolated_file(tmp_path, monkeypatch)
    before = p1._append_directory_exclusions("hotels in Toronto")
    assert "-site:zenhotels.com" not in before

    ld.record_learned_directory("zenhotels.com", reason="rule-based DIRECTORY")

    after = p1._append_directory_exclusions("hotels in Toronto")
    assert "-site:zenhotels.com" in after


def test_query_stays_well_formed_with_no_exclusions(tmp_path, monkeypatch):
    # Even with a populated hardcoded registry this can't be empty in
    # practice, but confirm the no-exclusions branch is at least a no-op
    # shape (same text back) when there's nothing to exclude.
    import discovery_classifier as dc
    monkeypatch.setattr(dc, "DISCOVERY_SOURCE_REGISTRY", {})
    _isolated_file(tmp_path, monkeypatch)
    result = p1._append_directory_exclusions("boutique hotels in Colorado")
    assert result == "boutique hotels in Colorado"
