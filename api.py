"""
AI BDM Platform - Phase 1 REST API
==================================

FastAPI layer over the Phase 1 pipeline (phase1_pipeline.py). Exposes the
query -> plan -> discover -> scrape -> store flow over HTTP, plus read access
to the persisted lead store.

Run:
  ./env/bin/python -m uvicorn api:app --reload --port 8000
  # interactive docs at http://127.0.0.1:8000/docs

Endpoints:
  GET  /health            - liveness probe
  POST /pipeline/run      - run the full Phase 1 pipeline for a query
  GET  /leads             - list stored leads (HTML omitted unless requested)
  GET  /leads/count       - number of stored leads
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

import phase1_pipeline as pipeline

app = FastAPI(
    title="AI BDM Platform - Phase 1 API",
    version="1.0.0",
    description="Natural-language lead query -> Maps discovery -> tiered scrape -> store.",
)


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


# --- Helpers ---------------------------------------------------------------
def _read_leads(include_text: bool, limit: Optional[int]) -> List[Dict[str, Any]]:
    """Read persisted leads from the crawl index (metadata) via the storage
    layer; page text is loaded from storage/<domain>/*.txt only when asked."""
    from storage import get_store
    store = get_store()
    leads: List[Dict[str, Any]] = []
    for row in store.read_index():
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
@app.get("/health", tags=["system"])
def health() -> Dict[str, str]:
    """Liveness probe; also reports which provider keys are configured."""
    return {
        "status": "ok",
        "serper_key": "set" if pipeline.SERPER_API_KEY else "missing",
        "scrapedo_key": "set" if pipeline.SCRAPEDO_API_KEY else "missing",
    }


@app.post("/pipeline/run", tags=["pipeline"], dependencies=[Depends(require_api_key)])
async def run_pipeline_endpoint(req: PipelineRequest) -> Dict[str, Any]:
    """Run the full Phase 1 pipeline and return the structured summary.

    The summary mirrors the CLI output: the resolved plan, how many places
    were discovered, and per-lead statuses (scraped / cache_hit / failed /
    no_website). Page text is not returned here — fetch it from /leads.
    """
    summary = await pipeline.run_pipeline(
        req.query, concurrency=req.concurrency
    )
    if summary.get("blocked"):
        # Query rejected by the moderation gate -- a client-side problem
        # with the REQUEST itself, not a server/upstream failure, so 400
        # (not 502) is the correct status. Detail is deliberately short and
        # generic -- the specific category/reason stays in the server log
        # (see moderate_user_query), not handed back to the caller.
        raise HTTPException(
            status_code=400,
            detail="This request violates our usage policy and was not processed.",
        )
    if summary.get("error"):
        # Intent/LLM stage failed (e.g. provider down) -> surface as 502.
        raise HTTPException(status_code=502, detail=summary["error"])
    return summary


@app.get("/leads", response_model=List[LeadSummary], tags=["leads"],
         dependencies=[Depends(require_api_key)])
def list_leads(
    include_text: bool = Query(False, description="Include page text (large)."),
    limit: Optional[int] = Query(None, ge=1, le=1000),
) -> List[Dict[str, Any]]:
    """List persisted leads from the store (page text omitted by default)."""
    return _read_leads(include_text=include_text, limit=limit)


@app.get("/leads/count", tags=["leads"], dependencies=[Depends(require_api_key)])
def leads_count() -> Dict[str, int]:
    """Return the number of leads currently persisted."""
    return {"count": len(_read_leads(include_text=False, limit=None))}


@app.on_event("startup")
def _startup() -> None:
    """Warm the storage layer so the first request doesn't pay initialization."""
    from storage import get_store
    get_store()
