"""Offline tests for job_contracts.classify_error() -- mapping real,
previously-observed failure signals (the same ones docs/RUNBOOK.md's
"Known failure modes" table documents) onto the Laravel guide's 8-class
error taxonomy (Section 4.6).

No network, no LLM call -- pure string matching against known message
patterns.

Run: pytest -q tests/unit/test_error_taxonomy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import job_contracts as jc  # noqa: E402


def test_quota_exhausted_maps_to_quota_class_retryable():
    envelope = jc.classify_error("Monthly API calls limit reached: 1000", "cor_1")
    assert envelope.error_class == jc.ErrorClass.QUOTA
    assert envelope.retryable is True
    assert envelope.correlation_id == "cor_1"


def test_rate_limit_maps_to_rate_limit_class_with_retry_after():
    envelope = jc.classify_error(
        "Rate limit reached for model ... on tokens per minute (TPM)", "cor_2"
    )
    assert envelope.error_class == jc.ErrorClass.RATE_LIMIT
    assert envelope.retryable is True
    assert envelope.retry_after_seconds is not None


def test_auth_rejected_maps_to_authentication_class_not_retryable():
    envelope = jc.classify_error("Invalid API key provided", "cor_3")
    assert envelope.error_class == jc.ErrorClass.AUTHENTICATION
    assert envelope.retryable is False


def test_blocked_source_maps_to_blocked_source_class():
    envelope = jc.classify_error("Access denied -- bot protection triggered", "cor_4")
    assert envelope.error_class == jc.ErrorClass.BLOCKED_SOURCE
    assert envelope.retryable is False


def test_transient_network_failure_maps_to_transient_provider_retryable():
    envelope = jc.classify_error(
        "Cannot connect to host example.com:443 ssl:default [getaddrinfo failed]", "cor_5"
    )
    assert envelope.error_class == jc.ErrorClass.TRANSIENT_PROVIDER
    assert envelope.retryable is True


def test_moderation_block_maps_to_validation_not_retryable():
    envelope = jc.classify_error("blocked_by_moderation", "cor_6")
    assert envelope.error_class == jc.ErrorClass.VALIDATION
    assert envelope.retryable is False


def test_intent_failed_maps_to_transient_provider():
    # phase1_pipeline.run_pipeline sets summary["error"] = "intent_failed: <exc>"
    # when the LLM/network call for planning fails -- retryable, since it's
    # the same class of failure as any other provider hiccup.
    envelope = jc.classify_error("intent_failed: Groq API timeout after 30s", "cor_7")
    assert envelope.error_class == jc.ErrorClass.TRANSIENT_PROVIDER
    assert envelope.retryable is True


def test_unrecognized_message_falls_back_to_internal_not_retryable():
    envelope = jc.classify_error("Something bizarre and never-seen-before happened", "cor_8")
    assert envelope.error_class == jc.ErrorClass.INTERNAL
    assert envelope.retryable is False
    assert envelope.code == "research.internal"


def test_empty_message_with_exception_falls_back_to_internal():
    envelope = jc.classify_error("", "cor_9", exc=ValueError("boom"))
    assert envelope.error_class == jc.ErrorClass.INTERNAL
    assert "boom" in envelope.message


def test_classification_is_case_insensitive():
    envelope = jc.classify_error("MONTHLY API CALLS LIMIT REACHED", "cor_10")
    assert envelope.error_class == jc.ErrorClass.QUOTA
