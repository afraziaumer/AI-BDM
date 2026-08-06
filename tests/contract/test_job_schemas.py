"""Contract tests for the job request/progress/result/error schemas
(Laravel guide Section 4.9's "Contract" row).

No network, no LLM call. Two things are verified:
1. Realistic, sanitized examples actually validate against the Pydantic
   models in job_contracts.py.
2. The committed JSON Schema files under docs/backend-handoff/schemas/
   have not drifted from what the current models actually generate --
   this catches exactly the "docs say one thing, code does another"
   class of bug this project has hit repeatedly (see
   docs/KNOWN_LIMITATIONS.md's report/code discrepancies section).

Run: pytest -q tests/contract
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import job_contracts as jc  # noqa: E402

SCHEMAS_DIR = REPO_ROOT / "docs" / "backend-handoff" / "schemas"


def test_realistic_request_validates():
    payload = {
        "job_id": "job_01HXYZ",
        "tenant_ref": "org_01HABC",
        "correlation_id": "cor_01HDEF",
        "requested_at": "2026-08-06T12:00:00Z",
        "target": {
            "industry": "commercial cleaning",
            "locations": [{"country": "US", "region": "PA", "city": "Philadelphia"}],
            "company_size": {"min_employees": 10, "max_employees": 250},
        },
        "limits": {"max_prospects": 100, "max_pages_per_domain": 20},
        "features": {"maps": True, "reviews": True, "tech_stack": True},
    }
    req = jc.JobRequest.model_validate(payload)
    assert req.schema_version == "1.0"
    assert req.target.industry == "commercial cleaning"


def test_request_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        jc.JobRequest.model_validate({
            "job_id": "job_01",
            "tenant_ref": "org_01",
            # correlation_id missing
            "requested_at": "2026-08-06T12:00:00Z",
            "target": {"industry": "marinas"},
        })


def test_realistic_result_validates():
    payload = {
        "job_id": "job_01HXYZ",
        "correlation_id": "cor_01HDEF",
        "status": "partially_completed",
        "pipeline_version": "git:abc1234",
        "started_at": "2026-08-06T12:00:00Z",
        "completed_at": "2026-08-06T12:05:00Z",
        "counts": {"candidates": 143, "prospects": 82, "warnings": 4},
        "prospects": [
            {
                "source_identity": {"domain": "example.com"},
                "company": {"name": "Example Co", "website": "https://example.com"},
                "score": {"value": 78, "model_version": "relevance_scoring-v1"},
                "evidence_refs": ["artifact://storage/example.com/"],
                "warnings": [],
            }
        ],
        "artifact_manifest": [
            {"type": "crawl_index", "uri": "artifact://crawl_index.csv", "sha256": None}
        ],
        "errors": [],
        "warnings": [],
    }
    result = jc.JobResult.model_validate(payload)
    assert result.status == jc.JobStatus.PARTIALLY_COMPLETED
    assert result.prospects[0].source_identity.domain == "example.com"


def test_realistic_progress_event_validates():
    payload = {
        "job_id": "job_01HXYZ",
        "correlation_id": "cor_01HDEF",
        "stage": "discover_scrape",
        "completed_units": 3,
        "total_units": 10,
        "message_code": "round_3_complete",
        "timestamp": "2026-08-06T12:01:00Z",
        "last_heartbeat_at": "2026-08-06T12:01:00Z",
    }
    event = jc.ProgressEvent.model_validate(payload)
    assert event.stage == "discover_scrape"


def test_realistic_error_envelope_validates():
    payload = {
        "code": "research.provider_rate_limited",
        "error_class": "rate_limit",
        "message": "Research is temporarily delayed.",
        "retryable": True,
        "retry_after_seconds": 120,
        "fields": {},
        "correlation_id": "cor_01HDEF",
    }
    envelope = jc.ErrorEnvelope.model_validate(payload)
    assert envelope.error_class == jc.ErrorClass.RATE_LIMIT


@pytest.mark.parametrize("filename,model", [
    ("request.schema.json", jc.JobRequest),
    ("progress.schema.json", jc.ProgressEvent),
    ("result.schema.json", jc.JobResult),
    ("error.schema.json", jc.ErrorEnvelope),
])
def test_committed_schema_file_matches_current_model(filename, model):
    """Regenerates the schema in-memory and compares against the committed
    file (ignoring the two identity keys added at generation time, $id and
    $schema, which aren't part of the model's own shape). A mismatch means
    someone edited job_contracts.py without regenerating the schema files
    -- exactly the kind of drift this test exists to catch."""
    committed_path = SCHEMAS_DIR / filename
    assert committed_path.exists(), f"{committed_path} is missing -- was it deleted?"
    with open(committed_path, "r", encoding="utf-8") as f:
        committed = json.load(f)
    committed.pop("$id", None)
    committed.pop("$schema", None)

    current = model.model_json_schema()

    assert committed == current, (
        f"{filename} is stale. Regenerate it from job_contracts.{model.__name__} "
        "and re-commit."
    )
