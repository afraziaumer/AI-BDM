"""HTTP-level tests for the thin Laravel entry point:
POST /api/v1/run-ai-bdm and GET /api/v1/ai-bdm/{job_id}.

These are pure reshaping over the existing job_runner machinery already
covered by test_api_jobs.py/test_job_lifecycle.py -- this file exercises
the new envelope shape and the raw_query bypass specifically, not the
underlying job execution logic again.

Run: pytest -q tests/mocked/test_api_run_ai_bdm.py
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


def test_no_api_key_rejected(client):
    async def scenario():
        async with client as c:
            return await c.post("/api/v1/run-ai-bdm", json={"query": "5 marinas in Dubai"})
    resp = asyncio.run(scenario())
    assert resp.status_code == 401


def test_missing_query_is_a_422(client):
    async def scenario():
        async with client as c:
            return await c.post("/api/v1/run-ai-bdm", json={}, headers=HEADERS)
    resp = asyncio.run(scenario())
    assert resp.status_code == 422


def test_submit_captures_the_raw_query_verbatim(client, monkeypatch):
    captured = {}

    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        captured["query"] = query
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            submit = await c.post(
                "/api/v1/run-ai-bdm",
                json={"query": "5 marinas in Dubai with no CRM"},
                headers=HEADERS,
            )
            job_id = submit.json()["job_id"]
            await job_runner._JOBS[job_id].task
            return submit, job_id

    submit, job_id = asyncio.run(scenario())
    assert submit.status_code == 202
    body = submit.json()
    assert body == {"success": True, "job_id": job_id, "status": "queued"}
    assert captured["query"] == "5 marinas in Dubai with no CRM"


def test_status_endpoint_reports_progress_while_running(client, monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        if progress_cb:
            progress_cb("discover_scrape", 2, 5, "round_complete")
        await asyncio.sleep(0.05)
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            submit = await c.post(
                "/api/v1/run-ai-bdm", json={"query": "5 marinas in Dubai"}, headers=HEADERS,
            )
            job_id = submit.json()["job_id"]
            await asyncio.sleep(0.01)
            mid = await c.get(f"/api/v1/ai-bdm/{job_id}", headers=HEADERS)
            await job_runner._JOBS[job_id].task
            return mid

    mid = asyncio.run(scenario())
    assert mid.status_code == 200
    body = mid.json()
    assert body["success"] is True
    assert body["status"] in ("queued", "running")
    assert "progress" in body


def test_status_endpoint_returns_results_on_completion(client, monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            submit = await c.post(
                "/api/v1/run-ai-bdm", json={"query": "5 marinas in Dubai"}, headers=HEADERS,
            )
            job_id = submit.json()["job_id"]
            await job_runner._JOBS[job_id].task
            return await c.get(f"/api/v1/ai-bdm/{job_id}", headers=HEADERS), job_id

    resp, job_id = asyncio.run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "success": True,
        "job_id": job_id,
        "status": "completed",
        "results": [
            {"company_name": "A Co", "website": "https://a.com", "domain": "a.com", "score": 85},
        ],
    }


def test_status_endpoint_reports_failure_as_success_false(client, monkeypatch):
    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        return {"error": "intent_failed: provider down", "blocked": False}
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            submit = await c.post(
                "/api/v1/run-ai-bdm", json={"query": "5 marinas in Dubai"}, headers=HEADERS,
            )
            job_id = submit.json()["job_id"]
            await job_runner._JOBS[job_id].task
            return await c.get(f"/api/v1/ai-bdm/{job_id}", headers=HEADERS), job_id

    resp, job_id = asyncio.run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["job_id"] == job_id
    assert body["status"] == "failed"
    assert "provider down" in body["error"]


def test_unknown_job_id_returns_404(client):
    async def scenario():
        async with client as c:
            return await c.get("/api/v1/ai-bdm/nope", headers=HEADERS)
    resp = asyncio.run(scenario())
    assert resp.status_code == 404


def test_count_overrides_the_default_limit(client, monkeypatch):
    captured = {}

    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        captured["limit"] = limit
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            submit = await c.post(
                "/api/v1/run-ai-bdm",
                json={"query": "marinas in Dubai", "count": 50},
                headers=HEADERS,
            )
            job_id = submit.json()["job_id"]
            await job_runner._JOBS[job_id].task

    asyncio.run(scenario())
    assert captured["limit"] == 50


def test_no_count_leaves_limit_unset_so_the_query_s_own_wording_governs(client, monkeypatch):
    captured = {}

    async def fake_run_pipeline(query, limit=None, progress_cb=None, cancel_check=None):
        captured["limit"] = limit
        return _success_summary()
    monkeypatch.setattr(job_runner.phase1_pipeline, "run_pipeline", fake_run_pipeline)

    async def scenario():
        async with client as c:
            submit = await c.post(
                "/api/v1/run-ai-bdm", json={"query": "5 marinas in Dubai"}, headers=HEADERS,
            )
            job_id = submit.json()["job_id"]
            await job_runner._JOBS[job_id].task

    asyncio.run(scenario())
    assert captured["limit"] is None
