"""HTTP-level tests for api.py's async job routes and the Idempotency-Key
mechanism on POST /api/v1/pipeline/run.

tests/mocked/test_job_lifecycle.py exercises job_runner.py's own logic
directly (no HTTP); this file exercises the actual FastAPI endpoints,
which is where a real bug was found during development: the job routes
must be `async def`, not `def`, or job_runner.submit_job()'s
asyncio.create_task() call fails with "no running event loop" (FastAPI
runs sync handlers in a worker thread with no loop of its own). Uses a
real httpx.AsyncClient against an ASGI transport (one shared event loop)
rather than Starlette's TestClient, whose threaded event-loop portal
produces misleading timing for background asyncio.create_task() work --
see docs/backend-handoff/JOB_EXECUTION.md's Verification section.

Run: pytest -q tests/mocked/test_api_jobs.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("AIBDM_API_KEY", "test-key-for-pytest")

import httpx  # noqa: E402
import pytest  # noqa: E402

import api  # noqa: E402
import job_runner  # noqa: E402

HEADERS = {"X-API-Key": "test-key-for-pytest"}


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


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=api.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _job_body(job_id: str, **overrides) -> dict:
    body = {
        "job_id": job_id, "tenant_ref": "org_1", "correlation_id": f"cor_{job_id}",
        "requested_at": "2026-08-06T12:00:00Z",
        "target": {"industry": "marinas", "locations": [{"country": "US", "city": "Miami"}]},
        "limits": {"max_prospects": 5},
    }
    body.update(overrides)
    return body


def test_no_api_key_rejected(client, monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            r = await c.get("/api/v1/jobs/whatever")
            return r
    resp = asyncio.run(scenario())
    assert resp.status_code == 401


def test_submit_status_result_lifecycle(client, monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        if progress_cb:
            progress_cb("planning", 1, 1, "plan_resolved")
        # A real await, not just a synchronous return -- without this the
        # background task can finish before the "still running" check
        # below even executes, since there's no actual delay to preempt on.
        await asyncio.sleep(0.05)
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            submit = await c.post("/api/v1/jobs", json=_job_body("job_http_lifecycle"), headers=HEADERS)
            early_result = await c.get("/api/v1/jobs/job_http_lifecycle/result", headers=HEADERS)
            await job_runner._JOBS["job_http_lifecycle"].task
            late_status = await c.get("/api/v1/jobs/job_http_lifecycle", headers=HEADERS)
            late_result = await c.get("/api/v1/jobs/job_http_lifecycle/result", headers=HEADERS)
            return submit, early_result, late_status, late_result

    submit, early_result, late_status, late_result = asyncio.run(scenario())

    assert submit.status_code == 202
    assert submit.json()["status"] == "queued"
    assert early_result.status_code == 202  # still running, not an error
    assert late_status.status_code == 200
    assert late_status.json()["status"] == "completed"
    assert late_result.status_code == 200
    body = late_result.json()
    assert body["status"] == "completed"
    assert body["prospects"][0]["source_identity"]["domain"] == "a.com"


def test_duplicate_submit_returns_409(client, monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            body = _job_body("job_http_dup")
            r1 = await c.post("/api/v1/jobs", json=body, headers=HEADERS)
            r2 = await c.post("/api/v1/jobs", json=body, headers=HEADERS)
            await job_runner._JOBS["job_http_dup"].task
            return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1.status_code == 202
    assert r2.status_code == 409


def test_unknown_job_id_returns_404(client):
    async def scenario():
        async with client as c:
            r_status = await c.get("/api/v1/jobs/nope", headers=HEADERS)
            r_result = await c.get("/api/v1/jobs/nope/result", headers=HEADERS)
            r_cancel = await c.post("/api/v1/jobs/nope/cancel", headers=HEADERS)
            return r_status, r_result, r_cancel

    r_status, r_result, r_cancel = asyncio.run(scenario())
    assert r_status.status_code == 404
    assert r_result.status_code == 404
    assert r_cancel.status_code == 404


def test_cancel_endpoint(client, monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        for i in range(5):
            if cancel_check and cancel_check():
                return _success_summary(cancelled=True)
            await asyncio.sleep(0.02)
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            await c.post("/api/v1/jobs", json=_job_body("job_http_cancel"), headers=HEADERS)
            await asyncio.sleep(0.03)
            r_cancel = await c.post("/api/v1/jobs/job_http_cancel/cancel", headers=HEADERS)
            await job_runner._JOBS["job_http_cancel"].task
            r_result = await c.get("/api/v1/jobs/job_http_cancel/result", headers=HEADERS)
            return r_cancel, r_result

    r_cancel, r_result = asyncio.run(scenario())
    assert r_cancel.status_code == 200
    assert r_cancel.json()["cancelled"] is True
    assert r_result.json()["status"] == "cancelled"


def test_resume_endpoint_reuses_original_target(client, monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            await c.post("/api/v1/jobs", json=_job_body("job_http_resume_orig"), headers=HEADERS)
            await job_runner._JOBS["job_http_resume_orig"].task
            r_resume = await c.post(
                "/api/v1/jobs/job_http_resume_orig/resume",
                json={"job_id": "job_http_resume_new", "correlation_id": "cor_resume_new"},
                headers=HEADERS,
            )
            await job_runner._JOBS["job_http_resume_new"].task
            r_result = await c.get("/api/v1/jobs/job_http_resume_new/result", headers=HEADERS)
            return r_resume, r_result

    r_resume, r_result = asyncio.run(scenario())
    assert r_resume.status_code == 202
    assert r_result.json()["resumed_from"] == "job_http_resume_orig"
    assert job_runner._JOBS["job_http_resume_new"].request.target.industry == "marinas"


def test_resume_unknown_original_returns_404(client):
    async def scenario():
        async with client as c:
            return await c.post(
                "/api/v1/jobs/does_not_exist/resume",
                json={"job_id": "job_x", "correlation_id": "cor_x"},
                headers=HEADERS,
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 404


def test_pipeline_run_idempotency_key_replays_without_rerunning(client, monkeypatch):
    call_count = {"n": 0}

    async def fake_run_pipeline(query, concurrency=5):
        call_count["n"] += 1
        return {"query": query, "discovered": 1, "qualified": [], "results": [], "error": None, "blocked": False}
    monkeypatch.setattr(api.pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            headers = {**HEADERS, "Idempotency-Key": "idem-test-1"}
            r1 = await c.post("/api/v1/pipeline/run", json={"query": "find 3 dental clinics"}, headers=headers)
            r2 = await c.post("/api/v1/pipeline/run", json={"query": "find 3 dental clinics"}, headers=headers)
            r3 = await c.post("/api/v1/pipeline/run", json={"query": "find 3 dental clinics"}, headers=HEADERS)
            return r1, r2, r3

    r1, r2, r3 = asyncio.run(scenario())
    assert r1.status_code == 200 and r2.status_code == 200 and r3.status_code == 200
    assert r1.json() == r2.json()
    assert call_count["n"] == 2  # first call + the no-Idempotency-Key call; the replay didn't re-run


def test_pipeline_run_idempotency_key_replays_error_outcome_too(client, monkeypatch):
    async def fake_run_pipeline(query, concurrency=5):
        return {"query": query, "error": "intent_failed: simulated", "blocked": False}
    monkeypatch.setattr(api.pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            headers = {**HEADERS, "Idempotency-Key": "idem-test-error"}
            r1 = await c.post("/api/v1/pipeline/run", json={"query": "find 3 dental clinics"}, headers=headers)
            r2 = await c.post("/api/v1/pipeline/run", json={"query": "find 3 dental clinics"}, headers=headers)
            return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1.status_code == 502
    assert r2.status_code == 502
    assert r1.json() == r2.json()
