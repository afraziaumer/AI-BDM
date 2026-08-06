"""Live provider credential/quota checks. Every test here is marked
@pytest.mark.live and is EXCLUDED from the default test run (see pytest.ini's
addopts = -m "not live") -- ordinary `pytest`, `pytest -q tests/smoke`,
`pytest -q tests/unit`, and `pytest -q tests/mocked` runs never touch these
or consume any credential's quota.

Run explicitly, only when you actually want to spend real provider
quota/credit verifying credentials:
    pytest -m live tests/integration

Each test skips (not fails) when its required credential isn't configured,
so this file is also safe to leave in place for a contributor who hasn't
set up every provider.

Costs, so you know before running:
    Groq /models     -- free (metadata only, no token usage)
    ScrapingBee usage -- free (account info, not a scrape)
    Apify users/me    -- free (account info, not an actor run)
    Serper search     -- costs exactly 1 real search credit
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.live


def _env(*names: str) -> str:
    """Same lowercase-first, uppercase-fallback convention the app itself
    uses (see .env.example) -- checks each name in order, returns the
    first one set."""
    for name in names:
        val = os.getenv(name)
        if val:
            return val
    return ""


@pytest.fixture(autouse=True, scope="module")
def _load_dotenv():
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")


def test_groq_key_is_valid():
    """Free -- lists available models, no token usage."""
    import requests
    key = _env("groq_llm_apikey1", "GROQ_API_KEY")
    if not key:
        pytest.skip("no Groq key configured")
    resp = requests.get(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {key}"}, timeout=15,
    )
    assert resp.status_code == 200, f"Groq key rejected: {resp.status_code} {resp.text[:200]}"
    assert len(resp.json().get("data", [])) > 0


def test_scrapingbee_key_and_quota():
    """Free -- account usage info, not a real scrape. Prints remaining
    quota so a developer can see at a glance whether the premium fetch
    tier is currently usable (see docs/KNOWN_LIMITATIONS.md's note on
    quota exhaustion)."""
    import requests
    key = _env("zenrows", "ZENROWS_API_KEY")
    if not key:
        pytest.skip("no ScrapingBee key configured (.env var 'zenrows')")
    resp = requests.get(
        "https://app.scrapingbee.com/api/v1/usage",
        params={"api_key": key}, timeout=15,
    )
    assert resp.status_code == 200, f"ScrapingBee key rejected: {resp.status_code} {resp.text[:200]}"
    data = resp.json()
    used, cap = data.get("used_api_credit", 0), data.get("max_api_credit", 0)
    print(f"\nScrapingBee quota: {used}/{cap} used (renews {data.get('renewal_subscription_date')})")
    assert "max_api_credit" in data


def test_apify_token_is_valid():
    """Free -- account info, not an actor run."""
    import requests
    token = _env("apify", "APIFY_API_TOKEN")
    if not token:
        pytest.skip("no Apify token configured")
    resp = requests.get(
        "https://api.apify.com/v2/users/me", params={"token": token}, timeout=15,
    )
    assert resp.status_code == 200, f"Apify token rejected: {resp.status_code} {resp.text[:200]}"
    assert resp.json().get("data", {}).get("username")


@pytest.mark.live
def test_serper_key_is_valid_costs_one_credit():
    """COSTS 1 real Serper search credit -- Serper has no free account-info
    endpoint, a real search is the only way to validate the key. Kept as
    the smallest, cheapest possible query."""
    import requests
    key = _env("serper", "SERPER_API_KEY")
    if not key:
        pytest.skip("no Serper key configured")
    resp = requests.post(
        "https://google.serper.dev/search",
        json={"q": "test", "num": 1},
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        timeout=15,
    )
    assert resp.status_code == 200, f"Serper key rejected: {resp.status_code} {resp.text[:200]}"
