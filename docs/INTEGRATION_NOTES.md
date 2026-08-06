# Integration Notes — Calling AI-BDM From an External System

This documents the current, real integration boundary — not a target design. Where a gap exists between what a Laravel backend would need and what exists today, it's called out explicitly rather than papered over.

## Current state, honestly

**An API already exists (`api.py`), but it's partial.** It's a FastAPI wrapper, versioned under `/api/v1` (Laravel guide, Section 5), exposing:
- `POST /api/v1/pipeline/run` → runs Stage 1 (planning) + Stage 2 (discovery/scraping) **synchronously** and returns `phase1_pipeline.run_pipeline()`'s structured summary dict directly. Blocks the HTTP request for the whole run.
- `POST /api/v1/jobs` / `GET /api/v1/jobs/{job_id}` / `GET /api/v1/jobs/{job_id}/result` / `POST /api/v1/jobs/{job_id}/cancel` → the same Stage 1–2 work, but async: submit returns immediately with a `job_id`, poll for progress/heartbeat, cancel mid-run, fetch the versioned `JobResult` once terminal. This is the guide's Section 4.3–4.6 contract — see `docs/backend-handoff/JOB_EXECUTION.md` for exact scope and status mapping.
- `GET /api/v1/leads` → cursor-paginated (`items` + opaque `next_cursor`) read-only access to whatever is currently persisted in `crawl_index.csv`. `GET /api/v1/leads/count` returns the total.
- `GET /api/v1/health` → liveness probe, also reports which provider keys are configured.

**Stages 3–8 (routing, final reasoning, tech-stack detection, evidence retrieval, Maps enrichment, accuracy audit) are not exposed by ANY of the above today** — neither the synchronous endpoint nor the async job API cover them. They only run as part of `main.py`'s `run()` function, which is a **print-based CLI orchestrator with no structured return value** (`async def run(...) -> None`) — it writes results to files (`leads_clean.csv`, `leads_with_maps.csv`, `accuracy_report.txt`) and prints progress to stdout, but does not currently return a JSON-serializable result object the way the Stage 1–2 endpoints do.

**This is the real integration gap.** Wrapping `main.py`'s full 8-stage orchestration into something with the same "returns a structured result" contract Stages 1–2 already have is real, scoped, achievable work — not yet done. A job requesting `features.maps`/`features.tech_stack` via `POST /api/v1/jobs` gets an explicit warning in its result rather than silently-ignored fields.

## Recommended near-term step (not yet built)

Refactor `main.py`'s `run()` to return a structured result dict (mirroring `phase1_pipeline.run_pipeline()`'s existing `summary` shape, extended with each later stage's output) instead of only printing, then extend `api.py` with a second endpoint that calls it. The pieces this depends on already exist and are stable:
- `phase1_pipeline.run_pipeline()` already returns a structured summary (Stages 1–2).
- `route_planner.plan_routes()`, `final_reasoning.answer_query()`, `phase3.google_maps.enrich()`, and `accuracy_check.run()` each already return/write structured data — they just aren't currently assembled into one combined return value by `main.py`.

## Job/result schema — now implemented, for Stages 1–2

The `job_id`/`tenant_ref`/`target` request shape and `prospects`/`evidence_refs`/`artifact_manifest` result shape the Laravel guide proposes are implemented in `job_contracts.py` and exposed via `POST /api/v1/jobs`. This section used to describe a hypothetical field mapping; it's real now, so the mapping lives in code and in `docs/backend-handoff/JOB_EXECUTION.md` instead of being duplicated here. Two things worth knowing that aren't obvious from the schema files alone:

- **The request's `target.industry`/`target.locations` gets translated into one natural-language sentence** (`job_translation.target_to_query()`) before it ever reaches `LLM_planner.plan_query()` — there is still no structured-field planner path, this translation layer is the bridge. `target.company_size` has no equivalent anywhere in this pipeline and is dropped with an explicit warning, not silently ignored.
- **The result's `prospects[].score` and `evidence_refs` are real**, sourced from `relevance_scoring.py`'s actual score and a `storage/<domain>/` artifact reference — but there is no `maps` field on a prospect, because Stage 7 (Google Maps enrichment) isn't part of what this job runner wraps. A job requesting `features.maps: true` gets a result-level warning instead of a fabricated `maps` object.

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
