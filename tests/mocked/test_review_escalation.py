"""Mocked tests for _fetch_apify_google_reviews()'s escalation loop --
targets 25 reviews WITH actual written text (not just 25 raw items), doubling
the request size when the first batch comes up short, stopping early either
when the provider's pool is genuinely exhausted (fewer raw items returned
than requested) or a spend ceiling is hit.

The Apify HTTP call is fully mocked (a fake aiohttp session/response) --
no real network call, no real API token needed.

Run: pytest -q tests/mocked
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


class _FakeResponse:
    def __init__(self, status, items):
        self.status = status
        self._items = items

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._items

    async def text(self):
        return str(self._items)


class _FakeSession:
    """Each call to post() consumes the next canned response in `responses`,
    keyed by call order -- lets a test script exactly what each successive
    escalation round should return."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0
        self.requested_counts = []

    def post(self, url, params=None, json=None, timeout=None):
        self.requested_counts.append(json["maxReviews"])
        status, items = self._responses[self.call_count]
        self.call_count += 1
        return _FakeResponse(status, items)


def _raw_items(n_total, n_with_text):
    """n_total raw Apify dataset items, only the first n_with_text carrying
    real review text (the rest are star-only ratings with no text, which
    the real code correctly filters out)."""
    items = []
    for i in range(n_total):
        if i < n_with_text:
            items.append({"text": f"Review number {i}", "stars": 5, "name": f"Reviewer {i}"})
        else:
            items.append({"text": "", "stars": 4, "name": f"Reviewer {i}"})  # star-only, no text
    return items


def _run(session):
    import phase3.review_harvester as rh
    return asyncio.run(rh._fetch_apify_google_reviews(session, "https://maps.example/place"))


def test_escalates_and_reaches_target(monkeypatch):
    """First request (25) comes up short (20 with text); the pool clearly
    has more (25 raw returned == 25 requested), so it escalates to 50 and
    gets enough -- final result is trimmed to exactly the target."""
    import phase3.review_harvester as rh
    monkeypatch.setattr(rh, "APIFY_API_TOKEN", "fake-token")
    monkeypatch.setattr(rh, "MAX_REVIEWS_PER_PLATFORM", 25)
    monkeypatch.setattr(rh, "APIFY_MAX_REVIEWS_ATTEMPT", 100)

    session = _FakeSession([
        (200, _raw_items(25, 20)),   # round 1: requested 25, got 20 with text -- short
        (200, _raw_items(50, 30)),   # round 2: requested 50, got 30 with text -- enough
    ])
    reviews = _run(session)

    assert len(reviews) == 25  # trimmed to the target, not left at 30
    assert session.requested_counts == [25, 50]  # doubled exactly once


def test_stops_early_when_pool_is_exhausted(monkeypatch):
    """The provider returns FEWER raw items than requested -- that's a
    genuine "this business doesn't have any more reviews" signal, so it
    must stop immediately rather than asking again (which would just repeat
    the same, now-exhausted, run)."""
    import phase3.review_harvester as rh
    monkeypatch.setattr(rh, "APIFY_API_TOKEN", "fake-token")
    monkeypatch.setattr(rh, "MAX_REVIEWS_PER_PLATFORM", 25)
    monkeypatch.setattr(rh, "APIFY_MAX_REVIEWS_ATTEMPT", 100)

    session = _FakeSession([
        (200, _raw_items(10, 10)),  # requested 25, only 10 raw items exist at all
    ])
    reviews = _run(session)

    assert len(reviews) == 10  # short of the 25 target, but correctly stopped
    assert session.requested_counts == [25]  # never asked again


def test_stops_at_ceiling_without_reaching_target(monkeypatch):
    """The provider keeps returning exactly as many raw items as requested
    (implying there might always be more) but the with-text ratio never
    reaches the target -- must stop once the ceiling is hit rather than
    escalating forever."""
    import phase3.review_harvester as rh
    monkeypatch.setattr(rh, "APIFY_API_TOKEN", "fake-token")
    monkeypatch.setattr(rh, "MAX_REVIEWS_PER_PLATFORM", 25)
    monkeypatch.setattr(rh, "APIFY_MAX_REVIEWS_ATTEMPT", 100)

    # 20% of raw items have text at every scale: 25->5, 50->10, 100->20 -- never hits 25.
    session = _FakeSession([
        (200, _raw_items(25, 5)),
        (200, _raw_items(50, 10)),
        (200, _raw_items(100, 20)),
    ])
    reviews = _run(session)

    assert len(reviews) == 20  # short of target, but stopped at the ceiling
    assert session.requested_counts == [25, 50, 100]  # doubled twice, then hit the ceiling and stopped


def test_no_api_token_returns_empty_without_any_request(monkeypatch):
    import phase3.review_harvester as rh
    monkeypatch.setattr(rh, "APIFY_API_TOKEN", "")

    session = _FakeSession([])  # would raise IndexError if a request were attempted
    reviews = _run(session)
    assert reviews == []


def test_request_failure_returns_empty_not_partial(monkeypatch):
    """A non-200 response must degrade to an empty result, not raise --
    this is a best-effort enrichment, never something that should crash the
    caller."""
    import phase3.review_harvester as rh
    monkeypatch.setattr(rh, "APIFY_API_TOKEN", "fake-token")
    monkeypatch.setattr(rh, "MAX_REVIEWS_PER_PLATFORM", 25)

    session = _FakeSession([(500, {"error": "internal"})])
    reviews = _run(session)
    assert reviews == []
