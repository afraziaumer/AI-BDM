# Integration Notes — Calling AI-BDM From an External System

This documents the current, real integration boundary — not a target design. Where a gap exists between what a Laravel backend would need and what exists today, it's called out explicitly rather than papered over.

## Current state, honestly

**An API already exists (`api.py`), but it's partial.** It's a FastAPI wrapper, versioned under `/api/v1` (Laravel guide, Section 5), exposing only:
- `POST /api/v1/pipeline/run` → runs Stage 1 (planning) + Stage 2 (discovery/scraping) and returns `phase1_pipeline.run_pipeline()`'s structured summary dict directly.
- `GET /api/v1/leads` → cursor-paginated (`items` + opaque `next_cursor`) read-only access to whatever is currently persisted in `crawl_index.csv`. `GET /api/v1/leads/count` returns the total.
- `GET /api/v1/health` → liveness probe, also reports which provider keys are configured.

**Stages 3–8 (routing, final reasoning, tech-stack detection, evidence retrieval, Maps enrichment, accuracy audit) are not exposed by this API today.** They only run as part of `main.py`'s `run()` function, which is a **print-based CLI orchestrator with no structured return value** (`async def run(...) -> None`) — it writes results to files (`leads_clean.csv`, `leads_with_maps.csv`, `accuracy_report.txt`) and prints progress to stdout, but does not currently return a JSON-serializable result object the way `api.py`'s Stage 1–2 endpoint does.

**This is the real integration gap.** Wrapping `main.py`'s full 8-stage orchestration into something with the same "returns a structured result" contract `api.py` already has for Stages 1–2 is real, scoped, achievable work — not yet done.

## Recommended near-term step (not yet built)

Refactor `main.py`'s `run()` to return a structured result dict (mirroring `phase1_pipeline.run_pipeline()`'s existing `summary` shape, extended with each later stage's output) instead of only printing, then extend `api.py` with a second endpoint that calls it. The pieces this depends on already exist and are stable:
- `phase1_pipeline.run_pipeline()` already returns a structured summary (Stages 1–2).
- `route_planner.plan_routes()`, `final_reasoning.answer_query()`, `phase3.google_maps.enrich()`, and `accuracy_check.run()` each already return/write structured data — they just aren't currently assembled into one combined return value by `main.py`.

## Job/result schema — mapped to what this system actually produces

The Laravel backend guide proposes a `job_id`/`tenant_ref`/`target` request shape and a `prospects`/`evidence_refs`/`artifact_manifest` result shape. Mapped to AI-BDM's real, current field names (not a hypothetical rename):

**Conceptual request → today's actual input:**
```json
{
  "query": "find 3 dental clinics in Islamabad with no online booking",
  "concurrency": 10
}
```
This is exactly `api.py`'s existing `PipelineRequest` schema. A future `target`-object-style request (industry/locations/company_size as separate structured fields, per the Laravel guide's example) would need a translation layer in front of `LLM_planner.plan_query()` — today, the whole request is one natural-language string that the LLM itself decomposes into `geo_location`/`broad_industry`/etc. There is no structured-field request path today.

**Conceptual result → today's actual output**, assembled from real fields already produced by the pipeline (not currently returned as one JSON object — see the gap above):
```json
{
  "status": "qualified_count vs shortfall in phase1_pipeline's summary",
  "prospects": [
    {
      "source_identity": {"domain": "example.com"},
      "company": {"name": "from leads_with_maps.csv company_name"},
      "score": {"value": "relevance_score column", "verdict": "relevance_verdict column"},
      "maps": {"rating": "maps_rating", "review_count": "maps_rating_count", "matched": "maps_matched"},
      "evidence_refs": ["storage/<domain>/*.txt", "storage/<domain>/reviews/*.json"],
      "warnings": "accuracy_report.txt's flags for this business, if any"
    }
  ]
}
```

**Distinguishing absent/unknown/failure** — already real behavior worth preserving in any Laravel-facing contract, not something to design from scratch: this pipeline already distinguishes a genuinely empty provider response (cached, safe to trust) from a request failure (never cached — see `docs/ARCHITECTURE.md`'s Stage 2/7 resilience notes). A Laravel-facing result contract should preserve this distinction rather than collapsing both into an empty field.

## What Python must never own (per the Laravel guide's runtime boundary)

Preserved here as a hard constraint on any future integration work, not just documentation: Python performs research, scraping, enrichment, and scoring only. It must never hold outbound email/send authority, make final approval decisions, or be trusted by a caller to supply its own `tenant_id` for authorization — a Laravel layer resolving tenant membership from an authenticated session is Laravel's responsibility, not something Python can substitute for.

## If calling the existing partial API today

```powershell
python -m uvicorn api:app --reload --port 8000
# interactive docs at http://127.0.0.1:8000/docs
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/pipeline/run \
  -H "X-API-Key: $AIBDM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "find 3 dental clinics in Islamabad", "concurrency": 5}'
```

Returns `phase1_pipeline.run_pipeline()`'s summary dict directly (Stages 1–2 only — see the gap above for what's missing).

```bash
curl "http://127.0.0.1:8000/api/v1/leads?limit=50" -H "X-API-Key: $AIBDM_API_KEY"
# {"items": [...], "next_cursor": "NTA="}
curl "http://127.0.0.1:8000/api/v1/leads?limit=50&cursor=NTA=" -H "X-API-Key: $AIBDM_API_KEY"
# next page; next_cursor is null once there are no more rows
```
