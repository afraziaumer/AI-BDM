"""Tests for storage.LocalPageStore.read_index()'s handling of a corrupted
crawl_index.csv -- real gap found live (professional QA suite, AI-BDM-268
"corrupt CSV -> system handles corrupted data gracefully"): a genuinely
corrupted index file (invalid UTF-8, e.g. from a truncated/interrupted
write) raised an uncaught UnicodeDecodeError, propagating to every caller
(api.py's /leads, main.py, accuracy_check.py, final_reasoning.py) --
inconsistent with read_page_text() in the same class, which already caught
this exact failure mode. Fixed to degrade to an empty index, same as a
missing file, rather than crashing.

Run: pytest -q tests/unit/test_storage_corrupt_index.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import storage  # noqa: E402


def _store(tmp_path, index_path):
    return storage.LocalPageStore(root=str(tmp_path / "final"), index_path=str(index_path))


def test_missing_index_file_returns_empty_list(tmp_path):
    store = _store(tmp_path, tmp_path / "does_not_exist.csv")
    assert store.read_index() == []


def test_corrupted_invalid_utf8_index_returns_empty_list_not_a_crash(tmp_path):
    idx = tmp_path / "crawl_index.csv"
    idx.write_bytes(b"company_name,email\r\n\x80\x81\x82,bad@bytes.com\r\n")
    store = _store(tmp_path, idx)
    assert store.read_index() == []  # must not raise UnicodeDecodeError


def test_valid_index_still_reads_normally(tmp_path):
    idx = tmp_path / "crawl_index.csv"
    idx.write_text("company_name,email\r\nAcme Co,x@acme.com\r\n", encoding="utf-8")
    store = _store(tmp_path, idx)
    rows = store.read_index()
    assert rows == [{"company_name": "Acme Co", "email": "x@acme.com"}]
