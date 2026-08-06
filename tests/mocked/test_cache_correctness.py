"""Mocked tests proving a TRANSIENT provider failure (network/timeout/non-200)
is never cached as a permanent "no match"/"no mentions" -- while a GENUINE
empty response (the provider succeeded and truly found nothing) is safely
cached. Both were real bugs this session: a business with a confirmed
Google Maps match minutes earlier got silently locked into "no match" by
one bad network moment; the same bug existed independently in the Reddit
search path.

phase3.store.load/save and the underlying provider calls are all mocked --
no real network call, no real cache file written.

Run: pytest -q tests/mocked
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


class _SaveRecorder:
    def __init__(self):
        self.calls = []

    def save(self, domain, platform, record):
        self.calls.append((domain, platform, record))

    def load(self, domain, platform, max_age_days=None):
        return None  # always a cache miss, so the real lookup path always runs


# --------------------------------------------------------------------------- #
# phase3/google_maps.py
# --------------------------------------------------------------------------- #
def test_google_maps_transient_failure_is_not_cached(monkeypatch):
    import phase3.google_maps as gm

    recorder = _SaveRecorder()
    monkeypatch.setattr(gm, "store", recorder)

    async def fake_serper_places_failure(session, query, num=5):
        return None  # the real signal for "the request itself failed"

    monkeypatch.setattr(gm, "_serper_places", fake_serper_places_failure)

    result = asyncio.run(gm.enrich_domain(
        session=None, domain="example.org", name="Example Co",
        address="123 Main St", geo="Example City", phone="+15551234567",
    ))

    assert result["matched"] is False
    assert result.get("transient_failure") is True
    assert recorder.calls == [], "a transient failure must never be cached"


def test_google_maps_genuine_empty_response_is_cached(monkeypatch):
    """The provider call SUCCEEDED and genuinely found nothing -- this is
    real information, safe (and correct) to cache."""
    import phase3.google_maps as gm

    recorder = _SaveRecorder()
    monkeypatch.setattr(gm, "store", recorder)

    async def fake_serper_places_empty(session, query, num=5):
        return []  # the real signal for "succeeded, found nothing"

    monkeypatch.setattr(gm, "_serper_places", fake_serper_places_empty)

    result = asyncio.run(gm.enrich_domain(
        session=None, domain="example.org", name="Example Co",
        address="123 Main St", geo="Example City", phone="+15551234567",
    ))

    assert result["matched"] is False
    assert not result.get("transient_failure")
    assert len(recorder.calls) == 1, "a genuine empty result must be cached"
    assert recorder.calls[0][0] == "example.org"


# --------------------------------------------------------------------------- #
# phase3/review_harvester.py -- reddit platform
# --------------------------------------------------------------------------- #
def test_reddit_transient_failure_is_not_cached(monkeypatch):
    import phase3.review_harvester as rh

    recorder = _SaveRecorder()
    monkeypatch.setattr(rh, "store", recorder)

    async def fake_reddit_failure(session, name, geo):
        return None  # request failed

    monkeypatch.setattr(rh, "_fetch_apify_reddit_mentions", fake_reddit_failure)

    result = asyncio.run(rh.enrich_platform(
        session=None, domain="example.org", name="Example Co", address="",
        geo="Example City", platform="reddit", hosts=(),
    ))

    assert result["matched"] is False
    assert recorder.calls == [], "a transient Reddit search failure must never be cached"


def test_reddit_genuine_no_mentions_is_cached(monkeypatch):
    import phase3.review_harvester as rh

    recorder = _SaveRecorder()
    monkeypatch.setattr(rh, "store", recorder)

    async def fake_reddit_empty(session, name, geo):
        return []  # searched successfully, genuinely nothing found

    monkeypatch.setattr(rh, "_fetch_apify_reddit_mentions", fake_reddit_empty)

    result = asyncio.run(rh.enrich_platform(
        session=None, domain="example.org", name="Example Co", address="",
        geo="Example City", platform="reddit", hosts=(),
    ))

    assert result["matched"] is False
    assert len(recorder.calls) == 1, "a genuine empty Reddit search must be cached"
