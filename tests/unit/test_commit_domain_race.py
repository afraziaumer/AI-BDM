"""Tests for storage.LocalPageStore.commit_domain()'s directory-replace
safety -- real bug found live (professional QA suite, SEC-013 "concurrent
same business"): the old check-then-act sequence (`if final.exists():
shutil.rmtree(final)` then a separate `shutil.move(staged, final)`) let a
second commit of the SAME domain, landing while the first commit's `final`
still existed, get NESTED inside it by shutil.move() instead of replacing
it -- producing a corrupted storage/<domain>/<domain>/... layout. Fixed by
renaming any prior `final` out of the way first (atomic), then renaming the
new content in, so shutil.move() never targets a path that still exists.

Run: pytest -q tests/unit/test_commit_domain_race.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import storage  # noqa: E402


def _store(tmp_path):
    return storage.LocalPageStore(root=str(tmp_path / "final"), index_path=str(tmp_path / "index.csv"))


def test_first_commit_produces_a_clean_layout(tmp_path):
    store = _store(tmp_path)
    store.stage_page("acme.com", "home", "A content")
    store.commit_domain("acme.com")
    files = sorted(p.name for p in (tmp_path / "final" / "acme.com").iterdir())
    assert files == ["home.txt"]


def test_recommitting_the_same_domain_replaces_cleanly_not_nested(tmp_path):
    """The actual defect: committing the same domain twice (re-run, or two
    overlapping processes) used to nest the second commit's staged content
    INSIDE the already-existing final directory instead of replacing it."""
    store = _store(tmp_path)
    store.stage_page("acme.com", "home", "A content")
    store.commit_domain("acme.com")

    store.stage_page("acme.com", "home", "B content newer")
    store.commit_domain("acme.com")

    final_dir = tmp_path / "final"
    all_files = sorted(p.relative_to(final_dir) for p in final_dir.rglob("*") if p.is_file())
    assert len(all_files) == 1, f"corrupted (nested/duplicated) layout: {all_files}"
    assert (final_dir / "acme.com" / "home.txt").read_text() == "B content newer"
    # No nested storage/acme.com/acme.com/... directory left behind.
    assert not (final_dir / "acme.com" / "acme.com").exists()


def test_no_stale_temp_directories_left_behind_after_a_recommit(tmp_path):
    store = _store(tmp_path)
    store.stage_page("acme.com", "home", "A content")
    store.commit_domain("acme.com")
    store.stage_page("acme.com", "home", "B content")
    store.commit_domain("acme.com")

    stale_dirs = [p for p in (tmp_path / "final").iterdir() if ".stale-" in p.name]
    assert stale_dirs == [], f"stale temp dirs left behind: {stale_dirs}"


def test_committing_a_domain_that_never_existed_before_still_works(tmp_path):
    """Sanity check the fix didn't break the ordinary first-commit path
    (no prior `final` to rename out of the way at all)."""
    store = _store(tmp_path)
    store.stage_page("brand-new.com", "home", "fresh content")
    store.commit_domain("brand-new.com")
    assert (tmp_path / "final" / "brand-new.com" / "home.txt").read_text() == "fresh content"
