"""Mocked tests for job_runner.py's async job lifecycle (Laravel guide
Section 4.5: progress, heartbeat, cancellation; Section 4.4: result
assembly; Section 4.6: error mapping on failure).

phase1_pipeline.run_pipeline is replaced with a fake async function per
test -- these tests exercise job_runner.py's OWN logic (job store,
progress/cancel wiring, result assembly, status mapping), not the real
pipeline. Real-pipeline behavior is covered elsewhere (tests/unit,
tests/mocked/test_cache_correctness.py, etc.).

Each test awaits the job's background task directly (`record.task`)
rather than sleeping an arbitrary duration -- deterministic, and matches
how the async job lifecycle was manually verified against a real
httpx.AsyncClient during development (see
docs/backend-handoff/JOB_EXECUTION.md's Verification section).

Run: pytest -q tests/mocked/test_job_lifecycle.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import job_contracts as jc  # noqa: E402
import job_runner  # noqa: E402


def _request(job_id: str, **overrides) -> jc.JobRequest:
    base = dict(
        job_id=job_id, tenant_ref="org_1", correlation_id=f"cor_{job_id}",
        requested_at=jc.utc_now(),
        target=jc.Target(industry="marinas", locations=[jc.Location(country="US", city="Miami")]),
        limits=jc.Limits(max_prospects=5),
    )
    base.update(overrides)
    return jc.JobRequest(**base)


async def _run_and_await(job_id: str, request: jc.JobRequest) -> jc.JobResult:
    job_runner.submit_job(request)
    record = job_runner._JOBS[job_id]
    await record.task
    return job_runner.get_result(job_id)


def _success_summary(**overrides) -> dict:
    base = dict(
        discovered=1, shortfall=0,
        results=[{"website_url": "https://a.com", "classification": {"score": "85"}}],
        qualified=[{
            "company_name": "A Co", "website_url": "https://a.com",
            "email": "x@a.com", "phone": "N/A", "matched_include": [], "review_summary": {},
        }],
        error=None, blocked=False, needs_clarification=False,
    )
    base.update(overrides)
    return base


def test_job_completes_successfully(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        if progress_cb:
            progress_cb("planning", 1, 1, "plan_resolved")
        return _success_summary()

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    result = asyncio.run(_run_and_await("job_success_1", _request("job_success_1")))

    assert result.status == jc.JobStatus.COMPLETED
    assert len(result.prospects) == 1
    assert result.prospects[0].source_identity.domain == "a.com"
    assert result.prospects[0].score.value == 85
    assert result.prospects[0].score.state is None  # a real value needs no state explanation
    assert result.pipeline_version.startswith("git:")


def test_score_state_not_attempted_when_classification_missing(monkeypatch):
    """A qualified prospect whose results row has no classification score
    (e.g. the cached-reuse path skipped fresh scoring) gets an explicit
    NOT_ATTEMPTED state, not a bare null with no explanation."""
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary(
            results=[{"website_url": "https://a.com", "classification": {}}],
        )

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    result = asyncio.run(_run_and_await("job_score_gap_1", _request("job_score_gap_1")))

    assert result.prospects[0].score.value is None
    assert result.prospects[0].score.state == jc.DataState.NOT_ATTEMPTED


def test_score_state_unknown_when_no_matching_results_row(monkeypatch):
    """A qualified prospect with no corresponding row in summary["results"]
    at all is a genuine data-consistency surprise -- UNKNOWN, not
    NOT_ATTEMPTED (which implies we know scoring simply didn't run)."""
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary(results=[])  # qualified references a.com, but no results row for it

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    result = asyncio.run(_run_and_await("job_score_gap_2", _request("job_score_gap_2")))

    assert result.prospects[0].score.value is None
    assert result.prospects[0].score.state == jc.DataState.UNKNOWN


def test_job_reports_progress_before_completing(monkeypatch):
    progress_seen = []

    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        if progress_cb:
            progress_cb("discover_scrape", 1, 3, "round_1_complete")
        # Snapshot status mid-run, before this coroutine returns.
        progress_seen.append(job_runner.get_status("job_progress_1"))
        return _success_summary()

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    asyncio.run(_run_and_await("job_progress_1", _request("job_progress_1")))

    assert len(progress_seen) == 1
    assert progress_seen[0]["stage"] == "discover_scrape"
    assert progress_seen[0]["completed_units"] == 1
    assert progress_seen[0]["total_units"] == 3
    assert progress_seen[0]["status"] == "running"


def test_job_can_be_cancelled_mid_run(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        for i in range(5):
            if cancel_check and cancel_check():
                return _success_summary(discovered=i, cancelled=True)
            if progress_cb:
                progress_cb("discover_scrape", i, 5, f"round_{i}_complete")
            await asyncio.sleep(0.02)
        return _success_summary(discovered=5)

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        req = _request("job_cancel_1")
        job_runner.submit_job(req)
        await asyncio.sleep(0.03)
        accepted = job_runner.cancel_job("job_cancel_1")
        record = job_runner._JOBS["job_cancel_1"]
        await record.task
        return accepted, job_runner.get_result("job_cancel_1")

    accepted, result = asyncio.run(scenario())
    assert accepted is True
    assert result.status == jc.JobStatus.CANCELLED


def test_cancelling_an_already_finished_job_returns_false(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary()

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    asyncio.run(_run_and_await("job_finished_1", _request("job_finished_1")))
    assert job_runner.cancel_job("job_finished_1") is False


def test_duplicate_job_id_rejected(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary()

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        req = _request("job_dup_1")
        job_runner.submit_job(req)
        with pytest.raises(job_runner.DuplicateJobError):
            job_runner.submit_job(req)
        await job_runner._JOBS["job_dup_1"].task

    asyncio.run(scenario())


def test_unknown_job_id_raises_not_found():
    with pytest.raises(job_runner.JobNotFoundError):
        job_runner.get_status("no_such_job")
    with pytest.raises(job_runner.JobNotFoundError):
        job_runner.get_result("no_such_job")
    with pytest.raises(job_runner.JobNotFoundError):
        job_runner.cancel_job("no_such_job")


def test_unexpected_exception_produces_failed_result_with_internal_error(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        raise RuntimeError("simulated unexpected crash")

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    result = asyncio.run(_run_and_await("job_crash_1", _request("job_crash_1")))

    assert result.status == jc.JobStatus.FAILED
    assert len(result.errors) == 1
    assert result.errors[0].error_class == jc.ErrorClass.INTERNAL
    assert "simulated unexpected crash" in result.errors[0].message


def test_needs_clarification_maps_to_awaiting_input(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary(
            needs_clarification=True,
            clarification_questions=["Which city specifically?"],
            qualified=[], results=[],
        )

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    result = asyncio.run(_run_and_await("job_clarify_1", _request("job_clarify_1")))

    assert result.status == jc.JobStatus.AWAITING_INPUT
    assert any("Which city specifically?" in w for w in result.warnings)


def test_blocked_query_maps_to_failed_with_validation_error(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary(
            blocked=True, error="blocked_by_moderation",
            qualified=[], results=[],
        )

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    result = asyncio.run(_run_and_await("job_blocked_1", _request("job_blocked_1")))

    assert result.status == jc.JobStatus.FAILED
    assert result.errors[0].error_class == jc.ErrorClass.VALIDATION
    assert result.errors[0].retryable is False


def test_shortfall_maps_to_partially_completed(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary(shortfall=2)

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    result = asyncio.run(_run_and_await("job_shortfall_1", _request("job_shortfall_1")))
    assert result.status == jc.JobStatus.PARTIALLY_COMPLETED


def test_features_maps_true_produces_warning_in_final_result(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary()

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    req = _request("job_maps_warn_1", features=jc.Features(maps=True, reviews=True, tech_stack=False))
    result = asyncio.run(_run_and_await("job_maps_warn_1", req))

    assert any("features.maps=true" in w for w in result.warnings)


def test_resume_job_reuses_original_target_and_marks_lineage(monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary()

    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        original_req = _request(
            "job_resume_orig_1",
            target=jc.Target(industry="dental clinics", locations=[jc.Location(country="PK", city="Lahore")]),
        )
        job_runner.submit_job(original_req)
        await job_runner._JOBS["job_resume_orig_1"].task

        job_runner.resume_job("job_resume_orig_1", "job_resume_new_1", "cor_resume_new")
        await job_runner._JOBS["job_resume_new_1"].task
        return job_runner.get_result("job_resume_new_1")

    result = asyncio.run(scenario())
    assert result.resumed_from == "job_resume_orig_1"
    new_record = job_runner._JOBS["job_resume_new_1"]
    assert new_record.request.target.industry == "dental clinics"
    assert new_record.request.target.locations[0].city == "Lahore"
    assert new_record.request.tenant_ref == "org_1"  # inherited, not respecified


def test_resume_unknown_original_raises_not_found():
    with pytest.raises(job_runner.JobNotFoundError):
        job_runner.resume_job("no_such_original", "job_new", "cor_new")


def test_resume_duplicate_new_job_id_raises(monkeypatch):
    async def scenario():
        req = _request("job_resume_dup_orig")

        async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
            return _success_summary()

        # monkeypatch.setattr (not a raw attribute assignment) so this reverts
        # automatically after the test -- a raw `phase1_pipeline.run_pipeline =
        # ...` here previously leaked into every later test in the same pytest
        # process (real bug, found while running the professional QA suite:
        # tests/unit/test_query_input.py's new guard tests failed only when
        # run as part of the full suite, never in isolation, because this
        # test's fake had permanently replaced the real run_pipeline).
        monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)
        job_runner.submit_job(req)
        await job_runner._JOBS["job_resume_dup_orig"].task

        # A job already exists under the name we're about to reuse as the "new" id.
        job_runner.submit_job(_request("job_resume_dup_taken"))
        await job_runner._JOBS["job_resume_dup_taken"].task

        with pytest.raises(job_runner.DuplicateJobError):
            job_runner.resume_job("job_resume_dup_orig", "job_resume_dup_taken", "cor_x")

    asyncio.run(scenario())
