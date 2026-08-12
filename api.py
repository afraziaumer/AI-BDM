"""
AI BDM Platform - Phase 1 REST API
==================================

FastAPI layer over the Phase 1 pipeline (phase1_pipeline.py). Exposes the
query -> plan -> discover -> scrape -> store flow over HTTP, plus read access
to the persisted lead store.

Run:
  ./env/bin/python -m uvicorn api:app --reload --port 8000
  # interactive docs at http://127.0.0.1:8000/docs

All routes are versioned under /api/v1 (Laravel Backend Construction
Handoff Guide, Section 5: "Use /api/v1 ... changes remain backward-
compatible within v1"). There is no unversioned alias -- this is the
initial version, so there's nothing to stay compatible with yet.

Endpoints:
  GET  /api/v1/health            - liveness probe
  POST /api/v1/pipeline/run      - run the full Phase 1 pipeline for a query
                                    (synchronous -- blocks for the whole run)
  GET  /api/v1/leads             - cursor-paginated list of stored leads
  GET  /api/v1/leads/count       - number of stored leads
  POST /api/v1/jobs               - submit an async job (Laravel guide Section 4.3)
  GET  /api/v1/jobs/{job_id}      - job status + progress (Section 4.5)
  GET  /api/v1/jobs/{job_id}/result - job result once terminal (Section 4.4)
  POST /api/v1/jobs/{job_id}/cancel - request cancellation (Section 4.5)
  POST /api/v1/jobs/{job_id}/resume - submit a new job resuming this one's
                                       target/limits/features (Section 4.5's
                                       "Checkpoint" requirement)
  POST /api/v1/run-ai-bdm         - submit {"query": "..."} as an async job
                                     (thin wrapper over /jobs for a caller
                                     that has one sentence, not a structured
                                     target -- see LARAVEL_QUICKSTART.md)
  GET  /api/v1/ai-bdm/{job_id}    - progress while running, results once done

The /jobs/* routes and /pipeline/run are two INDEPENDENT ways to run the
same underlying pipeline -- /pipeline/run blocks the HTTP request for the
whole run (fine for a quick manual call); /jobs/* returns immediately with
a job_id and is meant for a caller that queues work and polls, per the
guide's "long work must not be performed inside a public HTTP request"
rule. Both exist; neither is deprecated by the other.

run-ai-bdm/ai-bdm are NOT a third pipeline path -- they're a thin envelope
over the exact same job_runner.py machinery /jobs already uses, for a
caller with one natural-language sentence instead of a structured target.
See docs/backend-handoff/LARAVEL_QUICKSTART.md for the full contract.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Response, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

import job_runner
import phase1_pipeline as pipeline
from job_contracts import JobRequest, Limits, utc_now

app = FastAPI(
    title="AI BDM Platform - Phase 1 API",
    version="1.0.0",
    description="Natural-language lead query -> Maps discovery -> tiered scrape -> store.",
)
router = APIRouter(prefix="/api/v1")


# --- Auth ------------------------------------------------------------------
# Every endpoint except /health requires the header  X-API-Key: <AIBDM_API_KEY>.
# The key is read from the environment (never hard-coded). If it is NOT set, the
# protected endpoints refuse all calls (fail closed) rather than running open.
_API_KEY = os.getenv("AIBDM_API_KEY")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(provided: Optional[str] = Security(_api_key_header)) -> None:
    """Reject requests without a valid X-API-Key. Constant-time comparison."""
    if not _API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Server API key not configured (set AIBDM_API_KEY).",
        )
    if not provided or not secrets.compare_digest(provided, _API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


# --- Request / response schemas -------------------------------------------
class PipelineRequest(BaseModel):
    # The count comes from the query itself ("give me 50 marinas..."); the
    # planner extracts it (default 20). No separate limit field.
    query: str = Field(
        ..., min_length=3, examples=["give me 50 marinas in Dubai with no crm"]
    )
    concurrency: int = Field(5, ge=1, le=20, description="Parallel scrape workers.")


class RunAiBdmRequest(BaseModel):
    """Body for POST /api/v1/run-ai-bdm -- the thin, Laravel-facing entry
    point. Deliberately just `query` (+ optional `count`), not the full
    structured JobRequest shape /jobs expects: this exists specifically for
    a caller that has one natural-language sentence (same shape
    /pipeline/run already takes) and wants it run asynchronously instead
    of holding the HTTP connection open for the whole scrape."""
    query: str = Field(
        ..., min_length=3, examples=["5 marinas in Dubai with no CRM"]
    )
    count: Optional[int] = Field(
        None, ge=1, le=500,
        description="Overrides how many businesses to find. Omit to let the "
                     "query itself decide (e.g. 'find 50 marinas...' -> 50).",
    )


class ResumeRequest(BaseModel):
    """Body for POST /api/v1/jobs/{job_id}/resume. The caller supplies a
    NEW job_id (Laravel owns ID generation, same as job creation) --
    Python never invents its own job identifiers."""
    job_id: str = Field(..., min_length=1, description="New job_id for the resumed run.")
    correlation_id: str = Field(..., min_length=1)
    tenant_ref: Optional[str] = Field(
        None, description="Defaults to the original job's tenant_ref if omitted."
    )


class LeadSummary(BaseModel):
    company_name: str
    website_url: str
    page_url: str
    page_title: str = ""
    meta_description: str = ""
    email: str
    phone_number: str
    physical_address: str
    scrape_source_method: str
    text_length: int
    page_text: Optional[str] = None


class LeadsPage(BaseModel):
    """Cursor-paginated response for GET /api/v1/leads.

    `next_cursor` is an opaque token -- never a raw row index a client
    could infer meaning from or reconstruct out of band (Laravel guide,
    Section 5: "Identifiers: opaque string IDs; never expose sequential
    database IDs as a cross-system contract"). `None` means there is no
    next page.
    """
    items: List[LeadSummary]
    next_cursor: Optional[str] = None


# --- Helpers ---------------------------------------------------------------
def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> int:
    """Opaque cursor -> row offset. Invalid/tampered cursors fail closed
    with a 400, never silently fall back to offset 0 (which would look
    like a valid empty/first page instead of a client error)."""
    if not cursor:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor.") from exc
    if offset < 0:
        raise HTTPException(status_code=400, detail="Invalid cursor.")
    return offset


# --- Idempotency (Laravel guide, Section 5: "Idempotency-Key for commands
# that create jobs, approve, send, publish or perform provider actions") --
# POST /api/v1/jobs already has an idempotency key of its own -- job_id
# (a duplicate submission is rejected with 409, see job_runner.submit_job).
# POST /api/v1/pipeline/run had NO such protection at all: a client retry
# (e.g. after a client-side timeout, with the server call having actually
# succeeded) would silently trigger a full second pipeline run. This store
# closes that specific gap using the generic Idempotency-Key header the
# guide names, replaying the exact prior outcome (status code + body) for
# a repeated key instead of re-executing.
#
# Real, stated limitation: in-process, no TTL, no eviction -- grows for
# the life of the process. Acceptable for this handoff pass; a production
# deployment would want this backed by a real cache with expiry (e.g.
# Redis), same caveat as job_runner.py's in-process job store.
_idempotency_store: Dict[str, Dict[str, Any]] = {}

# Real gap found live (professional QA suite, AI-BDM-334 "concurrent
# same-key requests -- only one underlying operation should execute"):
# _idempotency_store only ever holds a COMPLETED result, so two requests
# with the SAME Idempotency-Key arriving truly concurrently (not
# sequentially, e.g. a client firing a retry before the first attempt's
# response even comes back) both pass the lookup-finds-nothing check and
# both run the full pipeline -- reproduced live, call count 2 for 2
# concurrent requests with one key. This set reserves a key for the
# duration of an in-flight request (checked/set synchronously, with no
# `await` between the check and the add, so it can't itself race within
# one process) -- a concurrent duplicate is rejected with 409 instead of
# triggering a second expensive run.
_idempotency_inflight: set = set()


def _fingerprint_request(payload: Dict[str, Any]) -> str:
    """A stable hash of the request body, used to detect the same
    Idempotency-Key being reused with a DIFFERENT payload (QA suite
    AI-BDM-115). Confirmed live before this fix existed: reusing a key with
    a different query silently replayed the FIRST request's cached result
    for the second, unrelated query -- exactly the "silently mix requests"
    failure mode the spec calls out. json.dumps with sort_keys gives a
    stable ordering regardless of dict insertion order."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _idempotency_lookup(
    endpoint: str, key: Optional[str], request_fingerprint: str
) -> Optional[Dict[str, Any]]:
    if not key:
        return None
    entry = _idempotency_store.get(f"{endpoint}:{key}")
    if entry is None:
        return None
    if entry["fingerprint"] != request_fingerprint:
        # Same key, different request body -- fail closed (never silently
        # replay a cached result for a request that isn't actually the one
        # that produced it) rather than either mixing requests or quietly
        # re-running the new one under someone else's idempotency key.
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with a different request body.",
        )
    return entry


def _idempotency_remember(
    endpoint: str, key: Optional[str], request_fingerprint: str, status_code: int, body: Any
) -> None:
    if key:
        _idempotency_store[f"{endpoint}:{key}"] = {
            "status_code": status_code, "body": body, "fingerprint": request_fingerprint,
        }


def _read_leads(
    include_text: bool, limit: Optional[int], offset: int = 0
) -> List[Dict[str, Any]]:
    """Read persisted leads from the crawl index (metadata) via the storage
    layer; page text is loaded from storage/<domain>/*.txt only when asked.

    Row order is `crawl_index.csv`'s own file order, which is append-only
    and stable across calls -- the deterministic sort order cursor
    pagination requires (Laravel guide, Section 5), as long as no row is
    reordered or deleted mid-pagination.
    """
    from storage import get_store
    store = get_store()
    leads: List[Dict[str, Any]] = []
    for i, row in enumerate(store.read_index()):
        if i < offset:
            continue
        text_length = int(row.get("content_length") or 0)
        leads.append(
            {
                "company_name": row.get("company_name", "N/A"),
                "website_url": row.get("website_url", "N/A"),
                "page_url": row.get("page_url", row.get("website_url", "N/A")),
                "page_title": row.get("page_title", ""),
                "meta_description": row.get("meta_description", ""),
                "email": row.get("email", "N/A"),
                "phone_number": row.get("phone_number", "N/A"),
                "physical_address": row.get("physical_address", "N/A"),
                "scrape_source_method": row.get("http_status", "N/A"),
                "text_length": text_length,
                "page_text": (store.read_page_text(row.get("txt_path", ""))
                              if include_text else None),
            }
        )
        if limit is not None and len(leads) >= limit:
            break
    return leads


# --- Routes ----------------------------------------------------------------
@router.get("/health", tags=["system"])
def health() -> Dict[str, str]:
    """Liveness probe; also reports which provider keys are configured."""
    return {
        "status": "ok",
        "serper_key": "set" if pipeline.SERPER_API_KEY else "missing",
        "premium_scraper_key": "set" if pipeline.ZENROWS_API_KEY else "missing",
    }


@router.post("/pipeline/run", tags=["pipeline"], dependencies=[Depends(require_api_key)])
async def run_pipeline_endpoint(
    req: PipelineRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> Dict[str, Any]:
    """Run the full Phase 1 pipeline and return the structured summary.

    The summary mirrors the CLI output: the resolved plan, how many places
    were discovered, and per-lead statuses (scraped / cache_hit / failed /
    no_website). Page text is not returned here — fetch it from /leads.

    An `Idempotency-Key` header is optional but recommended for any caller
    that might retry (e.g. after a client-side timeout) -- a repeated call
    with the same key replays the original outcome (success or error, same
    status code) instead of running the whole pipeline a second time.
    """
    fingerprint = _fingerprint_request(req.model_dump())
    cached = _idempotency_lookup("pipeline_run", idempotency_key, fingerprint)
    if cached is not None:
        response.status_code = cached["status_code"]
        return cached["body"]

    inflight_key = f"pipeline_run:{idempotency_key}" if idempotency_key else None
    if inflight_key is not None:
        if inflight_key in _idempotency_inflight:
            raise HTTPException(
                status_code=409,
                detail="A request with this Idempotency-Key is already in progress.",
            )
        _idempotency_inflight.add(inflight_key)
    try:
        summary = await pipeline.run_pipeline(
            req.query, concurrency=req.concurrency
        )
        if summary.get("blocked"):
            # Query rejected by the moderation gate -- a client-side problem
            # with the REQUEST itself, not a server/upstream failure, so 400
            # (not 502) is the correct status. Detail is deliberately short and
            # generic -- the specific category/reason stays in the server log
            # (see moderate_user_query), not handed back to the caller.
            detail = "This request violates our usage policy and was not processed."
            _idempotency_remember("pipeline_run", idempotency_key, fingerprint, 400, {"detail": detail})
            raise HTTPException(status_code=400, detail=detail)
        if summary.get("error"):
            # Intent/LLM stage failed (e.g. provider down) -> surface as 502.
            _idempotency_remember("pipeline_run", idempotency_key, fingerprint, 502, {"detail": summary["error"]})
            raise HTTPException(status_code=502, detail=summary["error"])
        _idempotency_remember("pipeline_run", idempotency_key, fingerprint, 200, summary)
        return summary
    finally:
        if inflight_key is not None:
            _idempotency_inflight.discard(inflight_key)


@router.get("/leads", response_model=LeadsPage, tags=["leads"],
            dependencies=[Depends(require_api_key)])
def list_leads(
    include_text: bool = Query(False, description="Include page text (large)."),
    limit: int = Query(50, ge=1, le=1000, description="Page size."),
    cursor: Optional[str] = Query(
        None, description="Opaque cursor from a previous response's next_cursor."
    ),
) -> LeadsPage:
    """Cursor-paginated list of persisted leads (page text omitted by default).

    Fetches one extra row past `limit` to determine whether a next page
    exists, without returning it -- the standard cursor-pagination
    lookahead trick, avoiding a separate count query.
    """
    offset = _decode_cursor(cursor)
    rows = _read_leads(include_text=include_text, limit=limit + 1, offset=offset)
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor(offset + limit) if has_more else None
    return LeadsPage(items=items, next_cursor=next_cursor)


@router.get("/leads/count", tags=["leads"], dependencies=[Depends(require_api_key)])
def leads_count() -> Dict[str, int]:
    """Return the number of leads currently persisted."""
    return {"count": len(_read_leads(include_text=False, limit=None))}


# --- Async job routes (Laravel guide Sections 4.3-4.6) ----------------------
# All four are `async def`, not plain `def` -- FastAPI runs sync endpoint
# functions in a worker thread pool, which has no running asyncio event
# loop of its own. job_runner.submit_job() calls asyncio.create_task()
# internally and MUST run on the same event loop the background job task
# and its heartbeat task run on, or task creation fails outright (confirmed
# live: RuntimeError: no running event loop). Making these async runs them
# on the real event loop thread, same as the existing /pipeline/run route.
@router.post("/jobs", status_code=202, tags=["jobs"], dependencies=[Depends(require_api_key)])
async def submit_job(req: JobRequest) -> Dict[str, Any]:
    """Submit an async job and return immediately with its initial status.

    202 Accepted, not 200/201 -- the job has been accepted for background
    execution, not completed or created as a durable resource in the REST
    sense. A repeated call with the same job_id is a 409 Conflict, not a
    silent no-op or a second run -- job_id IS this endpoint's idempotency
    key (Laravel guide, Section 5).
    """
    try:
        job_runner.submit_job(req)
    except job_runner.DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job_runner.get_status(req.job_id)


@router.get("/jobs/{job_id}", tags=["jobs"], dependencies=[Depends(require_api_key)])
async def job_status(job_id: str) -> Dict[str, Any]:
    """Current status + progress for a submitted job."""
    try:
        return job_runner.get_status(job_id)
    except job_runner.JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id!r}") from exc


@router.get("/jobs/{job_id}/result", tags=["jobs"], dependencies=[Depends(require_api_key)])
async def job_result(job_id: str, response: Response) -> Dict[str, Any]:
    """The job's JobResult once it reaches a terminal status.

    Still-running jobs return 202 with the current status instead of a
    404/409 -- the job exists and is progressing normally, it's just not
    done; the caller should poll GET /jobs/{job_id} or retry this endpoint.
    """
    try:
        status = job_runner.get_status(job_id)
        result = job_runner.get_result(job_id)
    except job_runner.JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id!r}") from exc
    if result is None:
        response.status_code = 202
        return {"job_id": job_id, "status": status["status"], "detail": "Job is still running; poll again."}
    return result.model_dump(mode="json")


@router.post("/jobs/{job_id}/cancel", tags=["jobs"], dependencies=[Depends(require_api_key)])
async def cancel_job(job_id: str) -> Dict[str, Any]:
    """Request cancellation. `cancelled: false` means the job was already
    in a terminal state when this was called -- not an error."""
    try:
        cancelled = job_runner.cancel_job(job_id)
    except job_runner.JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id!r}") from exc
    return {"job_id": job_id, "cancelled": cancelled}


@router.post("/jobs/{job_id}/resume", status_code=202, tags=["jobs"], dependencies=[Depends(require_api_key)])
async def resume_job(job_id: str, req: ResumeRequest) -> Dict[str, Any]:
    """Resume a previously failed/cancelled/partially-completed job as a
    NEW job (`req.job_id`), reusing the original's target/limits/features.

    This re-runs from the start, not from a serialized mid-run checkpoint
    -- businesses the original job already committed are skipped for free
    by the pipeline's own existing per-business cache, which is what makes
    this a genuine (if coarse-grained) resume rather than a plain re-run.
    See docs/backend-handoff/JOB_EXECUTION.md for exactly what this does
    and doesn't guarantee.
    """
    try:
        job_runner.resume_job(job_id, req.job_id, req.correlation_id, tenant_ref=req.tenant_ref)
    except job_runner.JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id!r}") from exc
    except job_runner.DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job_runner.get_status(req.job_id)


# --- Thin Laravel entry point ------------------------------------------------
# Both routes below are pure reshaping over the EXISTING job_runner
# machinery above (submit_job/get_status/get_result) -- no new pipeline
# logic, no new job store, no new progress tracking. They exist only
# because /jobs' JobRequest wants a structured `target.industry` +
# `target.locations`, not the one-sentence `{"query": "..."}` shape a
# Laravel caller has; `raw_query` (job_contracts.py) bypasses that
# templating instead of re-deriving it. See docs/backend-handoff/
# LARAVEL_QUICKSTART.md for the caller-facing contract.
def _job_id_for_run_ai_bdm() -> str:
    return f"aibdm_{uuid.uuid4().hex[:20]}"


@router.post("/run-ai-bdm", status_code=202, tags=["jobs"], dependencies=[Depends(require_api_key)])
async def run_ai_bdm(req: RunAiBdmRequest) -> Dict[str, Any]:
    """Submit the pipeline for `req.query` as a background job and return
    immediately with its job_id -- the async entry point requested for the
    Laravel integration. Poll GET /api/v1/ai-bdm/{job_id} for progress and
    the eventual result.
    """
    job_id = _job_id_for_run_ai_bdm()
    job_req = JobRequest(
        job_id=job_id,
        tenant_ref="run-ai-bdm",
        correlation_id=job_id,
        requested_at=utc_now(),
        raw_query=req.query,
        limits=Limits(max_prospects=req.count),
    )
    job_runner.submit_job(job_req)
    status = job_runner.get_status(job_id)
    return {"success": True, "job_id": job_id, "status": status["status"]}


@router.get("/ai-bdm/{job_id}", tags=["jobs"], dependencies=[Depends(require_api_key)])
async def ai_bdm_status(job_id: str) -> Dict[str, Any]:
    """Status/progress while running; results once terminal. Same envelope
    shape (`success`/`job_id`/`status`) on every response so a caller can
    branch on one field regardless of where the job currently is.
    """
    try:
        status = job_runner.get_status(job_id)
    except job_runner.JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id!r}") from exc

    if status["status"] in ("queued", "running"):
        return {
            "success": True,
            "job_id": job_id,
            "status": status["status"],
            "progress": {
                "stage": status["stage"],
                "completed": status["completed_units"],
                "total": status["total_units"],
                "message": status["message_code"],
            },
        }

    result = job_runner.get_result(job_id)
    if result.status.value in ("failed", "cancelled"):
        error_message = (
            result.errors[0].message if result.errors
            else f"Job ended with status {result.status.value!r}."
        )
        return {
            "success": False,
            "job_id": job_id,
            "status": result.status.value,
            "error": error_message,
        }

    results = [
        {
            "company_name": p.company.name,
            "website": p.company.website,
            "domain": p.source_identity.domain,
            "score": p.score.value,
        }
        for p in result.prospects
    ]
    body: Dict[str, Any] = {
        "success": True,
        "job_id": job_id,
        "status": result.status.value,
        "results": results,
    }
    if result.warnings:
        # Covers both genuine warnings (e.g. an unhonored feature flag) AND
        # status == "awaiting_input", whose clarification questions are
        # threaded through as warnings by job_runner._build_result() --
        # surfaced here rather than silently dropped just because this
        # envelope is a reshape of the richer JobResult.
        body["warnings"] = result.warnings
    return body


app.include_router(router)


@app.on_event("startup")
def _startup() -> None:
    """Warm the storage layer so the first request doesn't pay initialization."""
    from storage import get_store
    get_store()
