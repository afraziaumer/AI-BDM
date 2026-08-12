"""Tests for tech_stack.py's pure detection/categorization/capability logic.

Includes a real defect found live (professional QA suite, AI-BDM-153 "one
engine failure -> other detection engines still contribute"): the legacy
Wappalyzer engine call inside analyze_raw_html() was not individually
guarded like the other three (extended/DNS/robots) -- a legacy-engine
exception propagated out of the whole function, discarding signals from the
other three engines even though none of them depend on the legacy engine's
output. Fixed by isolating it the same way.

Run: pytest -q tests/unit/test_tech_stack_detection.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import tech_stack as ts  # noqa: E402


# --------------------------------------------------------------------------- #
# analyze_raw_html() -- per-engine isolation
# --------------------------------------------------------------------------- #
def test_legacy_engine_failure_does_not_block_the_other_three(monkeypatch):
    monkeypatch.setattr(ts, "_run_wappalyzer_on_webpage", lambda webpage: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ts, "_extended_static_detections", lambda *a, **kw: {"Cloudflare": {"versions": [], "categories": ["CDN"], "confidence": 100}})
    monkeypatch.setattr(ts, "_dns_based_detections", lambda domain: {})
    monkeypatch.setattr(ts, "_robots_based_detections", lambda url: {})

    class _FakeWebPage:
        def __init__(self, *a, **kw):
            pass
    monkeypatch.setattr(ts, "WebPage", _FakeWebPage, raising=False)
    import wappalyzer
    monkeypatch.setattr(wappalyzer, "WebPage", _FakeWebPage, raising=False)

    result = ts.analyze_raw_html("example.com", "<html></html>", {}, [])
    assert "Cloudflare" in result, f"extended-engine signal was lost when the legacy engine failed: {result}"


def test_all_engines_working_normally_still_merge(monkeypatch):
    monkeypatch.setattr(ts, "_run_wappalyzer_on_webpage", lambda webpage: {"WordPress": {"versions": [], "categories": ["CMS"], "confidence": 100}})
    monkeypatch.setattr(ts, "_extended_static_detections", lambda *a, **kw: {"Cloudflare": {"versions": [], "categories": ["CDN"], "confidence": 100}})
    monkeypatch.setattr(ts, "_dns_based_detections", lambda domain: {})
    monkeypatch.setattr(ts, "_robots_based_detections", lambda url: {})

    class _FakeWebPage:
        def __init__(self, *a, **kw):
            pass
    import wappalyzer
    monkeypatch.setattr(wappalyzer, "WebPage", _FakeWebPage, raising=False)

    result = ts.analyze_raw_html("example.com", "<html></html>", {}, [])
    assert set(result.keys()) == {"WordPress", "Cloudflare"}


# --------------------------------------------------------------------------- #
# run_rule_based_detection() -- Wappalyzer-independent keyword scan
# --------------------------------------------------------------------------- #
def test_rule_engine_detects_a_named_indicator():
    html = '<script src="https://js.stripe.com/v3/"></script>'
    findings = ts.run_rule_based_detection(html)
    names = {f["technology"] for f in findings}
    assert "Stripe" in names


def test_rule_engine_multiple_categories_all_detected():
    html = (
        '<script src="https://js.stripe.com/v3/"></script>'
        '<script src="https://widget.intercom.io/widget/abc"></script>'
        '<a href="/login">Sign in</a>'
    )
    findings = ts.run_rule_based_detection(html)
    names = {f["technology"] for f in findings}
    assert "Stripe" in names
    assert "Intercom" in names
    assert "Login/Sign-in page" in names


def test_rule_engine_finds_nothing_on_a_blank_page():
    assert ts.run_rule_based_detection("<html><body></body></html>") == []


def test_rule_engine_never_raises_on_garbage_input():
    assert ts.run_rule_based_detection(None) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# build_capabilities() / build_website_profile() -- no false positives when
# nothing was detected
# --------------------------------------------------------------------------- #
def test_empty_detection_produces_unknown_not_false_positives():
    profile = ts.build_website_profile("example.com", raw={}, discovered_urls=[])
    for name, cap in profile["capabilities"].items():
        assert cap["status"] == ts.STATUS_UNKNOWN, f"{name} unexpectedly detected from empty input"
    assert profile["detected_services"] == []
    assert profile["all_detections"] == []


def test_wappalyzer_match_takes_priority_over_rule_engine_for_the_same_bucket():
    raw = {"HubSpot": {"versions": [], "categories": ["CRM"], "confidence": 100}}
    rule_based = [{"technology": "Salesforce", "category": "crm", "version": None,
                   "confidence": 85, "source": "Rule Engine"}]
    profile = ts.build_website_profile("example.com", raw=raw, rule_based=rule_based)
    assert profile["capabilities"]["crm"]["technology"] == "HubSpot"
    assert profile["capabilities"]["crm"]["source"] == "wappalyzer"


def test_rule_engine_fills_a_bucket_wappalyzer_left_empty():
    rule_based = [{"technology": "Stripe", "category": "payments", "version": None,
                   "confidence": 85, "source": "Rule Engine"}]
    profile = ts.build_website_profile("example.com", raw={}, rule_based=rule_based)
    assert profile["capabilities"]["payments"]["status"] == ts.STATUS_DETECTED
    assert profile["capabilities"]["payments"]["provider"] == "Stripe"
    assert profile["capabilities"]["payments"]["source"] == "rule_engine"


def test_multiple_technologies_across_categories_all_appear_in_all_detections():
    raw = {
        "WordPress": {"versions": [], "categories": ["CMS"], "confidence": 100},
        "Google Analytics": {"versions": [], "categories": ["Analytics"], "confidence": 100},
    }
    rule_based = [{"technology": "Stripe", "category": "payments", "version": None,
                   "confidence": 85, "source": "Rule Engine"}]
    profile = ts.build_website_profile("example.com", raw=raw, rule_based=rule_based)
    all_names = {d["technology"] for d in profile["all_detections"]}
    assert {"WordPress", "Google Analytics", "Stripe"} <= all_names


# --------------------------------------------------------------------------- #
# detect_login_and_portal() -- URL-path + rule-engine + Wappalyzer signals
# --------------------------------------------------------------------------- #
def test_login_detected_from_discovered_url_path():
    has_login, has_portal = ts.detect_login_and_portal({}, ["https://example.com/login"])
    assert has_login is True
    assert has_portal is False


def test_portal_detected_from_discovered_url_path():
    has_login, has_portal = ts.detect_login_and_portal({}, ["https://example.com/customer-portal"])
    assert has_portal is True


def test_login_detected_from_rule_engine_when_no_url_path_hint():
    rule_based = [{"technology": "Login/Sign-in page", "category": "login_system",
                   "version": None, "confidence": 55, "source": "Rule Engine"}]
    has_login, _ = ts.detect_login_and_portal({}, [], rule_based=rule_based)
    assert has_login is True


def test_no_login_or_portal_when_nothing_indicates_it():
    has_login, has_portal = ts.detect_login_and_portal({}, ["https://example.com/about"])
    assert has_login is False
    assert has_portal is False


# --------------------------------------------------------------------------- #
# is_wordpress / detect_cms / detect_analytics -- boolean/list helpers
# --------------------------------------------------------------------------- #
def test_is_wordpress_true_when_wordpress_in_raw_wappalyzer(monkeypatch):
    monkeypatch.setattr(ts, "get_stored_profile", lambda domain: {"raw_wappalyzer": {"WordPress": {}}})
    assert ts.is_wordpress("example.com") is True


def test_is_wordpress_false_when_absent(monkeypatch):
    monkeypatch.setattr(ts, "get_stored_profile", lambda domain: {"raw_wappalyzer": {"Shopify": {}}})
    assert ts.is_wordpress("example.com") is False


def test_detect_cms_reads_the_normalized_bucket(monkeypatch):
    profile = {"normalized_tech_stack": {"cms": [{"name": "WordPress", "version": None, "confidence": 100, "source": "wappalyzer"}]}}
    monkeypatch.setattr(ts, "get_stored_profile", lambda domain: profile)
    result = ts.detect_cms("example.com")
    assert result[0]["name"] == "WordPress"
