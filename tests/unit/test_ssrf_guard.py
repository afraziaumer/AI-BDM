"""Tests for phase1_pipeline._resolves_to_blocked_address() -- SSRF guard.

Real vulnerability found while executing the proposed gap coverage
(GAP-SEC-002): every discovered/crawled URL was fetched with no
pre-flight check at all. A malicious search result or on-page link
pointing at http://169.254.169.254/ (cloud metadata) or a
localhost/private-range address was simply requested like any other URL --
on a cloud VM this would have returned real instance credentials. Fixed by
adding a pre-flight IP check (literal-IP and DNS-resolved) at the shared
entry point (execute_scavenger_scrape) and at the standalone _native_get(),
so a blocked address is dropped outright rather than escalated to a
different fetch tier.

Run: pytest -q tests/unit/test_ssrf_guard.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import phase1_pipeline as p1  # noqa: E402


def _blocked(url: str) -> bool:
    return asyncio.run(p1._resolves_to_blocked_address(url))


def test_cloud_metadata_address_is_blocked():
    assert _blocked("http://169.254.169.254/latest/meta-data/")


def test_loopback_literal_is_blocked():
    assert _blocked("http://127.0.0.1:22/")


def test_localhost_hostname_is_blocked():
    assert _blocked("http://localhost:445/")


def test_ipv6_loopback_is_blocked():
    assert _blocked("http://[::1]/")


def test_private_rfc1918_address_is_blocked():
    assert _blocked("http://10.0.0.5/internal")


def test_normal_external_url_is_not_blocked():
    assert not _blocked("https://example.com/")


def test_native_get_returns_none_for_blocked_address(monkeypatch):
    async def fail_if_called(*a, **kw):
        raise AssertionError("session.get() must never be called for a blocked address")

    class _FakeSession:
        get = staticmethod(fail_if_called)

    async def go():
        return await p1._native_get(_FakeSession(), "http://169.254.169.254/")

    assert asyncio.run(go()) is None


def test_scavenger_scrape_drops_blocked_address_without_escalating(monkeypatch):
    calls = []

    async def fake_tier1(session, url):
        calls.append("tier1")
        return None

    async def fake_tier2(session, url):
        calls.append("tier2")
        return {"html": "should never happen", "method": "PREMIUM"}

    monkeypatch.setattr(p1, "_TIER_SEQUENCE", (("normal", fake_tier1), ("premium", fake_tier2)))

    async def go():
        return await p1.execute_scavenger_scrape(None, "http://169.254.169.254/")

    result = asyncio.run(go())
    assert result["method"] == "FAILED"
    assert result["failure_reason"] == "blocked_internal_address"
    assert calls == []  # neither tier was ever invoked
