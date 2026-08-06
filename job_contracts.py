"""Versioned job request/progress/result/error contracts.

Implements Sections 4.3-4.6 of the Laravel Backend Construction Handoff
Guide: a structured job interface for wrapping the existing synchronous
pipeline (`phase1_pipeline.run_pipeline()`, orchestrated end-to-end by
`main.py`) as something a queue-based caller (Laravel) can submit, poll,
and cancel.

This module only defines the SHAPES. The actual translation from a
JobRequest into the pipeline's natural-language query lives in
job_translation.py; the actual async execution/progress/cancellation
lives in job_runner.py; the actual HTTP surface lives in api.py's
/api/v1/jobs/* routes. Keeping the contracts here, independent of any of
those, is what lets docs/backend-handoff/schemas/*.json be generated
directly from these models via `.model_json_schema()`.

schema_version "1.0" for every payload shape below -- bump this (not
these classes' Python names) if the wire shape ever changes incompatibly.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


# ===========================================================================
# 4.3 -- Job request
# ===========================================================================
class Location(BaseModel):
    country: str
    region: Optional[str] = None
    city: Optional[str] = None


class CompanySize(BaseModel):
    """Accepted on the wire per the guide's example -- NOT currently usable
    by this pipeline. There is no employee-count signal anywhere in the
    scrape/enrichment/scoring path. job_translation.target_to_query()
    surfaces this as an explicit warning on the job result rather than
    silently accepting and ignoring it."""
    min_employees: Optional[int] = None
    max_employees: Optional[int] = None


class Target(BaseModel):
    industry: str = Field(..., min_length=1)
    locations: List[Location] = Field(default_factory=list)
    company_size: Optional[CompanySize] = None


class Limits(BaseModel):
    max_prospects: int = Field(20, ge=1, le=500)
    max_pages_per_domain: int = Field(20, ge=1, le=100)


class Features(BaseModel):
    maps: bool = True
    reviews: bool = True
    tech_stack: bool = False


class JobRequest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str = Field(..., min_length=1)
    tenant_ref: str = Field(..., min_length=1)
    correlation_id: str = Field(..., min_length=1)
    requested_at: datetime
    target: Target
    limits: Limits = Field(default_factory=Limits)
    features: Features = Field(default_factory=Features)


# ===========================================================================
# 4.5 -- Progress
# ===========================================================================
class ProgressEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    correlation_id: str
    stage: str
    completed_units: Optional[int] = None
    total_units: Optional[int] = None
    message_code: str
    timestamp: datetime
    last_heartbeat_at: datetime


# ===========================================================================
# 4.4 -- Result and evidence contract
# ===========================================================================
class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    # Not in the guide's Section 4.4 result example, but matches the exact
    # name its own Section 3.6 frontend workflow-state table uses for a
    # research run that stopped because the request needs more detail
    # before it can run (this pipeline's clarification-question gate --
    # see phase1_pipeline.run_pipeline()'s needs_clarification handling).
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceIdentity(BaseModel):
    domain: str


class Company(BaseModel):
    name: str
    website: Optional[str] = None


class Score(BaseModel):
    value: Optional[int] = None
    model_version: str


class Prospect(BaseModel):
    source_identity: SourceIdentity
    company: Company
    score: Score
    evidence_refs: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ArtifactManifestEntry(BaseModel):
    type: str
    uri: str
    sha256: Optional[str] = None


# ===========================================================================
# 4.6 -- Error taxonomy
# ===========================================================================
class ErrorClass(str, Enum):
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    TRANSIENT_PROVIDER = "transient_provider"
    BLOCKED_SOURCE = "blocked_source"
    DATA_QUALITY = "data_quality"
    INTERNAL = "internal"


class ErrorEnvelope(BaseModel):
    code: str
    error_class: ErrorClass
    message: str
    retryable: bool
    retry_after_seconds: Optional[int] = None
    fields: Dict[str, str] = Field(default_factory=dict)
    correlation_id: str


class JobResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    correlation_id: str
    status: JobStatus
    pipeline_version: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    counts: Dict[str, int] = Field(default_factory=dict)
    prospects: List[Prospect] = Field(default_factory=list)
    artifact_manifest: List[ArtifactManifestEntry] = Field(default_factory=list)
    errors: List[ErrorEnvelope] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ===========================================================================
# classify_error() -- the actual taxonomy mapping
# ===========================================================================
# Ordered (name, class, retryable, retry_after_seconds, code_suffix, pattern)
# tuples. Matched against a raw error/log message via substring/regex --
# these are the SAME signal strings docs/RUNBOOK.md's "Known failure modes"
# table already documents as real, observed messages from this pipeline's
# providers; this is that table turned into a machine-checkable mapping,
# not a new invented taxonomy.
_CLASSIFICATION_RULES: List[tuple] = [
    (
        "moderation_blocked",
        ErrorClass.VALIDATION,
        False,
        None,
        r"blocked_by_moderation",
    ),
    (
        "quota_exhausted",
        ErrorClass.QUOTA,
        True,
        None,
        r"monthly api calls limit reached|quota exceeded|daily limit",
    ),
    (
        "rate_limited",
        ErrorClass.RATE_LIMIT,
        True,
        60,
        r"rate limit reached|too many requests|\b429\b",
    ),
    (
        "auth_rejected",
        ErrorClass.AUTHENTICATION,
        False,
        None,
        r"invalid api key|unauthorized|\b401\b|\b403\b|authentication failed",
    ),
    (
        "provider_blocked",
        ErrorClass.BLOCKED_SOURCE,
        False,
        None,
        r"bot.protection|captcha|access denied|cloudflare challenge|robots.*disallow",
    ),
    (
        "transient_network",
        ErrorClass.TRANSIENT_PROVIDER,
        True,
        None,
        r"getaddrinfo failed|cannot connect to host|timeout|connection reset|\b5\d\d\b|"
        r"temporarily unavailable",
    ),
    (
        "intent_failed",
        ErrorClass.TRANSIENT_PROVIDER,
        True,
        None,
        r"intent_failed",
    ),
]


def classify_error(
    raw_message: str, correlation_id: str, exc: Optional[BaseException] = None
) -> ErrorEnvelope:
    """Map a raw error/log message (and, when available, the exception that
    produced it) onto the 8-class taxonomy from Section 4.6.

    Falls back to ErrorClass.INTERNAL, not retryable, when nothing matches
    -- an unrecognized failure should surface as "something unexpected
    happened, don't loop on it automatically" rather than silently
    defaulting to a class that tells the caller to keep retrying.
    """
    text = (raw_message or "").lower()
    for name, error_class, retryable, retry_after, pattern in _CLASSIFICATION_RULES:
        if re.search(pattern, text):
            return ErrorEnvelope(
                code=f"research.{name}",
                error_class=error_class,
                message=raw_message,
                retryable=retryable,
                retry_after_seconds=retry_after,
                correlation_id=correlation_id,
            )
    return ErrorEnvelope(
        code="research.internal",
        error_class=ErrorClass.INTERNAL,
        message=raw_message or (str(exc) if exc else "Unknown error."),
        retryable=False,
        correlation_id=correlation_id,
    )


def utc_now() -> datetime:
    """Local alias kept here (not importing time_utils) so this module has
    zero project-internal dependencies -- it should be safely importable
    from anywhere, including a future standalone schema-generation script,
    without pulling in anything else."""
    return datetime.now(timezone.utc)
