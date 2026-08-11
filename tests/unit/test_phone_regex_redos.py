"""Tests for ReDoS protection on the LLM-generated phone_regex used by
phase1_pipeline.extract_contacts().

Real vulnerability found while executing the proposed gap coverage
(GAP-SEC-003): phone_regex comes from the query-planning LLM and is
compiled and run against scraped page text. MAX_PHONE_REGEX_LEN (a length
cap) and MAX_PHONE_SCAN_CHARS (a scan-length cap) do NOT bound execution
TIME -- a pattern as short as `(?:\\d+)+\\+` (12 chars) hung real
extraction for 10+ seconds on a 40-character input.

A first fix attempt (stdlib re compiled + run in a ThreadPoolExecutor with
future.result(timeout=...)) did NOT work: stdlib re's C matcher holds the
GIL for the whole backtracking search, so the waiting thread never even
gets scheduled to notice the timeout -- confirmed live, it still hung
indefinitely. The actual fix switches phone-regex compilation from stdlib
`re` to the third-party `regex` module (already an installed dependency),
whose `timeout=` parameter genuinely works, and calls it via
_safe_regex_findall().

Run: pytest -q tests/unit/test_phone_regex_redos.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import phase1_pipeline as p1  # noqa: E402


def test_pathological_regex_does_not_hang_extract_contacts():
    pathological = r"(?:\d+)+\+"
    text = "Call us: " + ("1" * 40) + "X for a quote"

    t0 = time.time()
    result = p1.extract_contacts(html="", text=text, country_code="US", phone_regex=pathological)
    elapsed = time.time() - t0

    assert elapsed < 5, f"extract_contacts took {elapsed:.2f}s -- ReDoS guard regressed"
    assert isinstance(result.get("phones"), list)


def test_pathological_nested_quantifier_is_bounded():
    pathological = r"(a+)+b"
    text = "a" * 200 + "X"  # no trailing 'b' -> forces maximal backtracking

    t0 = time.time()
    p1.extract_contacts(html="", text=text, country_code="US", phone_regex=pathological)
    elapsed = time.time() - t0

    assert elapsed < 5, f"took {elapsed:.2f}s -- ReDoS guard regressed"


def test_normal_llm_style_regex_still_extracts_real_phones():
    normal_regex = (
        r"(?:(?:\+1|1)[\s.\-]?)?(?<!\d)(?:\([2-9]\d{2}\)|[2-9]\d{2})"
        r"[\s.\-]?[2-9]\d{2}[\s.\-]?\d{4}(?!\d)"
    )
    text = "Contact us at (212) 555-0198 or via our sales line +1 415-555-0134 for a quote."
    result = p1.extract_contacts(html="", text=text, country_code="US", phone_regex=normal_regex)
    assert len(result.get("phones", [])) >= 1


def test_safe_regex_findall_falls_back_on_genuine_timeout(monkeypatch):
    """Force the TimeoutError branch deterministically (real timing is too
    flaky to rely on) and confirm it falls back to the known-safe pattern
    instead of propagating the error or hanging."""
    class _AlwaysTimesOut:
        def findall(self, text, timeout=None):
            raise TimeoutError

    text = "call +1 212 555 0198 now"
    result = p1._safe_regex_findall(_AlwaysTimesOut(), text)
    assert result == p1._PHONE_RE_FALLBACK.findall(text)
