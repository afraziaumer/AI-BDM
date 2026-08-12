"""Tests for phase1_pipeline._classify_fetch_failure() -- previously
untested in isolation despite being a pure function.

Run: pytest -q tests/unit/test_classify_fetch_failure.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import phase1_pipeline as p1  # noqa: E402


def test_cloudflare_challenge_body_detected_regardless_of_status():
    assert p1._classify_fetch_failure(200, "Just a moment... please wait") == "cloudflare_challenge"


def test_captcha_body_detected():
    assert p1._classify_fetch_failure(200, "Please solve this CAPTCHA to continue") == "captcha"


def test_js_challenge_body_detected():
    assert p1._classify_fetch_failure(503, "Checking your browser before accessing") == "js_challenge"


def test_http_403_with_no_signature_is_forbidden():
    assert p1._classify_fetch_failure(403, "Access Denied") == "forbidden_403"


def test_http_429_with_no_signature_is_rate_limited():
    assert p1._classify_fetch_failure(429, "Too many requests") == "rate_limited_429"


def test_http_500_with_no_signature_is_server_error():
    assert p1._classify_fetch_failure(500, "Internal Server Error") == "server_error"


def test_empty_200_response_is_flagged_as_empty_protected():
    assert p1._classify_fetch_failure(200, "   ") == "empty_protected_response"


def test_unrecognized_failure_falls_back_to_unknown_block():
    assert p1._classify_fetch_failure(418, "I'm a teapot") == "unknown_block"


def test_body_signature_wins_over_status_code_priority():
    """A block signature in the body is checked BEFORE the status code --
    a genuine Cloudflare page can arrive with a faked 200."""
    assert p1._classify_fetch_failure(200, "cf-browser-verification token here") == "cloudflare_challenge"
