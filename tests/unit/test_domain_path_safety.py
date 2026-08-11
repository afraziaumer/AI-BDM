"""Tests for domain_utils.safe_domain_component() -- path-traversal guard for
domain strings used as filesystem path segments.

Real vulnerability found while executing a professional QA test suite
(SEC-003, "unsafe business/domain string" / "Files remain within intended
storage structure"): domain_key() only strips slashes when its input is a
proper URL. A raw non-URL string like '..\\..\\windows\\system32' (or a
LLM-planned `target_domain` value, which is only strip/lower'd, never run
through domain_key()) passed straight through untouched. On Windows,
Path('storage') / '\\windows\\system32' resolves to C:\\Windows\\System32,
because a leading path separator makes pathlib treat the right operand as
rooted -- completely escaping the intended storage directory. Fixed by
routing every domain-to-path call site (storage.py, accuracy_check.py,
phase3/store.py, rag/ingest_reviews.py) through safe_domain_component(),
which strips all path separators and '..'/'.'  segments.

Run: pytest -q tests/unit/test_domain_path_safety.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from domain_utils import safe_domain_component  # noqa: E402
from storage import LocalPageStore  # noqa: E402


def _contained(root: Path, child: Path) -> bool:
    root, child = root.resolve(), child.resolve()
    return child == root or root in child.parents


def test_windows_backslash_traversal_is_neutralized():
    assert safe_domain_component("..\\..\\windows\\system32") == "windows_system32"


def test_leading_backslash_alone_is_neutralized():
    assert safe_domain_component("\\windows\\system32") == "windows_system32"


def test_unix_style_traversal_is_neutralized():
    assert safe_domain_component("../../../etc/passwd") == "etc_passwd"


def test_bare_dotdot_falls_back_to_placeholder():
    assert safe_domain_component("..") == "_"


def test_empty_string_falls_back_to_placeholder():
    assert safe_domain_component("") == "_"


def test_normal_domain_is_unaffected():
    assert safe_domain_component("example.com") == "example.com"


def test_local_page_store_staging_and_final_dir_stay_within_root(tmp_path):
    store = LocalPageStore(root=str(tmp_path / "storage"))
    malicious = "..\\..\\windows\\system32"
    assert _contained(store.root, store._staging_dir(malicious))
    assert _contained(store.root, store._final_dir(malicious))
