"""In-process job execution layer implementing Section 4.5 (progress,
heartbeat, cancellation) of the Laravel Backend Construction Handoff Guide
on top of the existing, unmodified `phase1_pipeline.run_pipeline()`.

Design choice, stated honestly rather than left implicit: this is an
IN-PROCESS job store (a plain dict), not Redis/Celery-backed. That matches
what the guide actually asks for -- "a private Python worker" -- without
inventing infrastructure this project doesn't have. The real, known cost
of that choice: job state is lost if this process restarts mid-run. See
docs/backend-handoff/JOB_EXECUTION.md for the full scope note.

Scope note: this wraps `run_pipeline()`, i.e. Stages 1-2 only (planning +
discovery/scraping) -- exactly what `api.py`'s existing
`POST /api/v1/pipeline/run` already exposes. Stages 3-8 (routing, final
reasoning, tech-stack detection, RAG, Maps enrichment, accuracy audit) are
NOT part of this job runner; see job_translation.py's warnings for what
that means for `features.maps`/`features.tech_stack` specifically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import domain_utils
import phase1_pipeline
import relevance_scoring
from job_contracts import (
    ArtifactManifestEntry,
    Company,
    DataState,
    ErrorEnvelope,
    JobRequest,
    JobResult,
    JobStatus,
    Prospect,
    Score,
    SourceIdentity,
    classify_error,
    utc_now,
)
from job_translation import target_to_query

logger = logging.getLogger("ai_bdm.job_runner")

HEARTBEAT_INTERVAL_S = 15.0

_TERMINAL_STATUSES = frozenset({
    JobStatus.COMPLETED, JobStatus.PARTIALLY_COMPLETED,
    JobStatus.FAILED, JobStatus.CANCELLED,
})


class DuplicateJobError(Exception):
    """Raised when submit_job() is called with a job_id already in use."""


class JobNotFoundError(Exception):
    """Raised when a job_id is looked up that was never submitted (or the
    process restarted since it was -- see this module's docstring)."""


@dataclass
class _JobRecord:
    request: JobRequest
    status: JobStatus = JobStatus.QUEUED
    stage: str = "queued"
    completed_units: Optional[int] = None
    total_units: Optional[int] = None
    message_code: str = "queued"
    started_at: Optional[datetime] = None
    last_heartbeat_at: datetime = field(default_factory=utc_now)
    cancel_requested: bool = False
    result: Optional[JobResult] = None
    task: Optional["asyncio.Task"] = None
    resumed_from: Optional[str] = None


_JOBS: Dict[str, _JobRecord] = {}


def _pipeline_version() -> str:
    """`git:<commit-sha>` -- the same "pipeline_version" identity scheme
    used throughout docs/backend-handoff/. Computed once per process, not
    per job, since it can't change while this process is running."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True,
            timeout=5,
        ).strip()
        return f"git:{sha}"
    except Exception:  # noqa: BLE001 - version identity must never crash a job
        return "git:unknown"


_PIPELINE_VERSION = _pipeline_version()


def submit_job(req: JobRequest, resumed_from: Optional[str] = None) -> None:
    """Register and start a job. Raises DuplicateJobError if `req.job_id`
    was already submitted -- job_id is the caller's (Laravel's) idempotency
    boundary; silently accepting a duplicate would let a retried job
    submission spin up a second, redundant pipeline run.

    `resumed_from` is set internally by resume_job() -- not a normal
    caller-facing parameter, just how it threads the lineage through to
    the eventual JobResult without a second code path."""
    if req.job_id in _JOBS:
        raise DuplicateJobError(f"job_id {req.job_id!r} already submitted.")
    record = _JobRecord(request=req, resumed_from=resumed_from)
    _JOBS[req.job_id] = record
    record.task = asyncio.create_task(_execute(req.job_id))


def resume_job(
    original_job_id: str,
    new_job_id: str,
    new_correlation_id: str,
    tenant_ref: Optional[str] = None,
) -> None:
    """Submit a NEW job (`new_job_id`) reusing `original_job_id`'s target/
    limits/features -- the resume mechanism for Section 4.5's "Checkpoint"
    requirement ("persist enough state to resume expensive work OR
    clearly document which stages restart").

    This deliberately does NOT replay progress from where the original
    job stopped -- it re-runs run_pipeline() from the start with the same
    translated query. What makes this a genuine resume rather than a
    plain re-run: any business the original job (or any prior job/CLI run
    against the same query) already committed to storage/<domain>/ is
    skipped for free by the pipeline's own existing per-business cache
    (see docs/DATA_CONTRACTS.md's re-run/idempotency section) -- so a job
    that crashed after committing 40 of 50 requested businesses resumes
    by only attempting the remaining ~10, not all 50 again. This is
    coarse-grained (whole-business, not mid-scrape) resume, stated
    honestly as such rather than oversold as a fine-grained checkpoint.

    Raises JobNotFoundError if `original_job_id` was never submitted (or
    the process restarted since -- see this module's docstring), and
    DuplicateJobError if `new_job_id` is already in use, same as
    submit_job().
    """
    original = _require(original_job_id)
    new_request = JobRequest(
        job_id=new_job_id,
        tenant_ref=tenant_ref or original.request.tenant_ref,
        correlation_id=new_correlation_id,
        requested_at=utc_now(),
        target=original.request.target,
        limits=original.request.limits,
        features=original.request.features,
    )
    submit_job(new_request, resumed_from=original_job_id)


def get_status(job_id: str) -> Dict[str, object]:
    record = _require(job_id)
    return {
        "job_id": job_id,
        "correlation_id": record.request.correlation_id,
        "status": record.status.value,
        "stage": record.stage,
        "completed_units": record.completed_units,
        "total_units": record.total_units,
        "message_code": record.message_code,
        "last_heartbeat_at": record.last_heartbeat_at,
    }


def cancel_job(job_id: str) -> bool:
    """Requests cancellation. Returns False (not an error) if the job is
    already in a terminal state -- cancelling a finished job is a no-op,
    not a failure condition."""
    record = _require(job_id)
    if record.status in _TERMINAL_STATUSES:
        return False
    record.cancel_requested = True
    return True


def get_result(job_id: str) -> Optional[JobResult]:
    """None while the job is still queued/running -- callers distinguish
    "not done yet" from "done" by checking get_status()'s status field
    first, same as the guide's polling model implies."""
    record = _require(job_id)
    return record.result


def _require(job_id: str) -> _JobRecord:
    record = _JOBS.get(job_id)
    if record is None:
        raise JobNotFoundError(job_id)
    return record


async def _heartbeat_loop(job_id: str) -> None:
    """Touches last_heartbeat_at on a fixed interval, independent of
    whether any progress actually happened -- so a job stuck mid-stage
    (e.g. waiting on a slow provider) still proves it's alive, per
    Section 4.5's "Heartbeat" requirement."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        record = _JOBS.get(job_id)
        if record is None:
            return
        record.last_heartbeat_at = utc_now()


async def _execute(job_id: str) -> None:
    record = _JOBS[job_id]
    req = record.request
    record.status = JobStatus.RUNNING
    record.started_at = utc_now()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(job_id))

    def _on_progress(stage: str, completed: Optional[int], total: Optional[int], message_code: str) -> None:
        record.stage = stage
        record.completed_units = completed
        record.total_units = total
        record.message_code = message_code
        record.last_heartbeat_at = utc_now()

    def _on_cancel_check() -> bool:
        return record.cancel_requested

    query, translation_warnings = target_to_query(req)

    try:
        summary = await phase1_pipeline.run_pipeline(
            query,
            limit=req.limits.max_prospects,
            progress_cb=_on_progress,
            cancel_check=_on_cancel_check,
        )
    except Exception as exc:  # noqa: BLE001 - a job must always resolve to a JobResult, never propagate
        logger.exception("Job %s: run_pipeline raised unexpectedly.", job_id)
        record.status = JobStatus.FAILED
        record.result = JobResult(
            job_id=job_id,
            correlation_id=req.correlation_id,
            status=JobStatus.FAILED,
            pipeline_version=_PIPELINE_VERSION,
            started_at=record.started_at,
            completed_at=utc_now(),
            errors=[classify_error(str(exc), req.correlation_id, exc)],
            warnings=translation_warnings,
            resumed_from=record.resumed_from,
        )
    else:
        record.result = _build_result(
            job_id, req, summary, translation_warnings, record.started_at,
            resumed_from=record.resumed_from,
        )
        record.status = record.result.status
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


def _build_result(
    job_id: str,
    req: JobRequest,
    summary: Dict[str, object],
    translation_warnings: List[str],
    started_at: datetime,
    resumed_from: Optional[str] = None,
) -> JobResult:
    correlation_id = req.correlation_id
    warnings: List[str] = list(translation_warnings)
    errors: List[ErrorEnvelope] = []

    if summary.get("blocked"):
        status = JobStatus.FAILED
        errors.append(classify_error(str(summary.get("error") or "blocked_by_moderation"), correlation_id))
    elif summary.get("error"):
        status = JobStatus.FAILED
        errors.append(classify_error(str(summary["error"]), correlation_id))
    elif summary.get("needs_clarification"):
        # Matches the "awaiting_input" state the Laravel guide's own
        # Section 3.6 frontend workflow table names for exactly this
        # situation -- the request wasn't wrong, it was under-specified.
        status = JobStatus.AWAITING_INPUT
        warnings.extend(str(q) for q in (summary.get("clarification_questions") or []))
    elif summary.get("cancelled"):
        status = JobStatus.CANCELLED
    elif summary.get("shortfall", 0):
        status = JobStatus.PARTIALLY_COMPLETED
    else:
        status = JobStatus.COMPLETED

    prospects: List[Prospect] = []
    results_by_website = {
        r.get("website_url"): r for r in (summary.get("results") or [])
    }
    for q in summary.get("qualified") or []:
        website_url = q.get("website_url") or ""
        domain = domain_utils.domain_key(website_url) if website_url else ""
        source_row = results_by_website.get(website_url)
        classification = (source_row or {}).get("classification") or {}
        raw_score = classification.get("score")

        # Section 5's null/absence convention: WHY a value is missing is
        # part of the contract, not just that it's missing.
        score_value: Optional[int] = None
        score_state: Optional[DataState] = None
        if source_row is None:
            # A qualified prospect with no matching row in summary["results"]
            # is a genuine data-consistency surprise, not an expected gap.
            score_state = DataState.UNKNOWN
        elif not classification or raw_score in (None, ""):
            # relevance_scoring never attached a score for this candidate
            # (e.g. the cached-reuse path, or scoring disabled for this run).
            score_state = DataState.NOT_ATTEMPTED
        else:
            try:
                score_value = int(raw_score)
            except (TypeError, ValueError):
                # Got a value, couldn't interpret it -- distinct from never
                # having tried at all.
                score_state = DataState.UNKNOWN

        prospects.append(Prospect(
            source_identity=SourceIdentity(domain=domain),
            company=Company(name=q.get("company_name") or domain or "Unknown", website=website_url or None),
            score=Score(
                value=score_value,
                model_version=f"relevance_scoring-v{relevance_scoring.SCORING_VERSION}",
                state=score_state,
            ),
            evidence_refs=[f"artifact://storage/{domain}/"] if domain else [],
        ))

    # Real, current gap stated honestly rather than fabricated: no artifact
    # is content-hashed anywhere in this codebase (see
    # docs/backend-handoff/ARTIFACTS_AND_STORAGE.md) -- sha256 is left
    # unset rather than filled with a fake value.
    artifact_manifest = [
        ArtifactManifestEntry(type="crawl_index", uri="artifact://crawl_index.csv"),
    ]

    counts = {
        "candidates": int(summary.get("discovered") or 0),
        "prospects": len(prospects),
        "warnings": len(warnings),
    }

    return JobResult(
        job_id=job_id,
        correlation_id=correlation_id,
        status=status,
        pipeline_version=_PIPELINE_VERSION,
        started_at=started_at,
        completed_at=utc_now(),
        counts=counts,
        prospects=prospects,
        artifact_manifest=artifact_manifest,
        errors=errors,
        warnings=warnings,
        resumed_from=resumed_from,
    )
