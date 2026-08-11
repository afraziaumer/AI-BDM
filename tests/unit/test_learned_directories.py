"""Tests for learned_directories.py -- the self-growing directory/OTA list.

Feature requested directly: "we can hardcode, but it should also work at
recognising new unknown sites and them adding them in list". Complements
discovery_classifier.DISCOVERY_SOURCE_REGISTRY (a hand-curated, hardcoded
list) rather than replacing it -- every time website_classifier.
classify_homepage() confidently determines a NEW domain is a directory,
that domain gets persisted here, so every future query excludes it
directly at Serper search time (see phase1_pipeline._append_directory_
exclusions) instead of re-discovering and re-classifying it from scratch.

Run: pytest -q tests/unit/test_learned_directories.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import learned_directories as ld  # noqa: E402


def _isolated_file(tmp_path, monkeypatch):
    path = str(tmp_path / "test_learned_directories.json")
    monkeypatch.setattr(ld, "LEARNED_DIRECTORIES_FILE", path)
    return path


def test_recording_a_new_domain_then_appears_in_exclusions(tmp_path, monkeypatch):
    _isolated_file(tmp_path, monkeypatch)
    assert "zenhotels.com" not in ld.load_for_exclusion()
    ld.record_learned_directory("zenhotels.com", reason="rule-based DIRECTORY")
    assert "zenhotels.com" in ld.load_for_exclusion()


def test_recording_the_same_domain_twice_does_not_duplicate(tmp_path, monkeypatch):
    path = _isolated_file(tmp_path, monkeypatch)
    ld.record_learned_directory("zenhotels.com", reason="first")
    ld.record_learned_directory("zenhotels.com", reason="second, refreshed")
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert list(data.keys()) == ["zenhotels.com"]
    assert data["zenhotels.com"]["reason"] == "second, refreshed"


def test_domain_is_normalized_to_lowercase(tmp_path, monkeypatch):
    _isolated_file(tmp_path, monkeypatch)
    ld.record_learned_directory("ZenHotels.COM", reason="x")
    assert "zenhotels.com" in ld.load_for_exclusion()


def test_blank_domain_is_a_safe_no_op(tmp_path, monkeypatch):
    path = _isolated_file(tmp_path, monkeypatch)
    ld.record_learned_directory("", reason="x")
    ld.record_learned_directory("   ", reason="x")
    assert not os.path.exists(path)


def test_load_for_exclusion_respects_the_limit(tmp_path, monkeypatch):
    _isolated_file(tmp_path, monkeypatch)
    for i in range(10):
        ld.record_learned_directory(f"site{i}.com", reason="x")
    assert len(ld.load_for_exclusion(limit=3)) == 3


def test_missing_file_returns_empty_set_not_an_error(tmp_path, monkeypatch):
    _isolated_file(tmp_path, monkeypatch)
    assert ld.load_for_exclusion() == set()


def test_persists_across_a_fresh_load(tmp_path, monkeypatch):
    """Simulates surviving a process restart -- a second, independent
    load_for_exclusion() call must see what an earlier call recorded."""
    _isolated_file(tmp_path, monkeypatch)
    ld.record_learned_directory("wego.com", reason="x")
    # A fresh call with no in-memory state carried over (module has none
    # beyond the file itself) must still see it.
    assert "wego.com" in ld.load_for_exclusion()
