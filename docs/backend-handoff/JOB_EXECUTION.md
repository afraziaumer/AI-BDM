# Job Execution

Implements Sections 4.3–4.6 of the Laravel Backend Construction Handoff Guide: a versioned job request/progress/result/error contract wrapping the existing pipeline. This document describes what's actually built, including its honest scope limits — not a target design.

## Scope, stated up front

This wraps `phase1_pipeline.run_pipeline()` — Stages 1–2 (planning, discovery/scraping) — the exact same scope `api.py`'s pre-existing `POST /api/v1/pipeline/run` already covers. **It does not run Stages 3–8** (routing, final reasoning, tech-stack detection, RAG, Maps enrichment, accuracy audit). A job requesting `features.maps: true` or `features.tech_stack: true` gets an explicit warning in its result, not silently-ignored fields or fabricated data — see `job_translation.py`.

The job store is **in-process** (a plain Python dict in `job_runner.py`), not Redis/Celery-backed. Real, known cost of that choice: job state is lost if this process restarts mid-run. Chosen deliberately over inventing infrastructure this project doesn't otherwise have — the guide asks for "a private Python worker," not a specific queue technology.

## The four pieces

| Guide section | File | What it is |
|---|---|---|
| 4.3 Request | `job_contracts.py` (`JobRequest`, `Target`, `Location`, `CompanySize`, `Limits`, `Features`) | The versioned request shape |
| 4.3→CLI bridge | `job_translation.py` (`target_to_query()`) | Converts the structured request into the one natural-language sentence `LLM_planner.plan_query()` actually understands, since there is still no structured-field planner path |
| 4.4 Result | `job_contracts.py` (`JobResult`, `Prospect`, `ArtifactManifestEntry`) | The versioned result shape |
| 4.5 Progress/cancellation | `phase1_pipeline.run_pipeline()`'s optional `progress_cb`/`cancel_check` params + `job_runner.py`'s heartbeat loop | Hooks into the existing discovery round-loop; a heartbeat task ticks independently every 15s |
| 4.6 Errors | `job_contracts.py` (`ErrorEnvelope`, `ErrorClass`, `classify_error()`) | Maps known failure signals (the same ones `docs/RUNBOOK.md`'s "Known failure modes" table already documents) onto the 8-class taxonomy |
| Execution | `job_runner.py` | In-process job store: `submit_job()`/`get_status()`/`cancel_job()`/`get_result()`/`resume_job()`, background `asyncio` task per job |
| HTTP surface | `api.py`, `/api/v1/jobs/*` | `POST /jobs`, `GET /jobs/{job_id}`, `GET /jobs/{job_id}/result`, `POST /jobs/{job_id}/cancel`, `POST /jobs/{job_id}/resume` |
| HTTP surface (raw-query) | `api.py`, `/api/v1/run-ai-bdm`, `/api/v1/ai-bdm/{job_id}` | Thin envelope over the same `job_runner.py` for a caller with one sentence instead of a structured `target` — see `LARAVEL_QUICKSTART.md` |

## Request → query translation, honestly

`target_to_query()` builds a sentence like `"find 25 commercial cleaning businesses in Philadelphia, PA, US"` from the structured request. `JobRequest.raw_query` bypasses this templating entirely — set it (instead of `target`) when the caller already has one natural-language sentence, and it's passed to the planner verbatim. `POST /api/v1/run-ai-bdm` (`LARAVEL_QUICKSTART.md`) always uses this path; forcing a raw sentence through `target.industry` instead would mangle it (the sentence would get wrapped inside the `"find {count} {industry} businesses"` template a second time). Exactly one of `target`/`raw_query` must be set — the request is rejected otherwise.

For the structured `target` path, three fields cannot be honestly represented and are surfaced as warnings on the eventual `JobResult`, never silently dropped or faked as working:

- **`target.company_size`** — no employee-count signal exists anywhere in this pipeline's scrape/enrichment/scoring path. Ignored, flagged.
- **`features.tech_stack`** — `needs_tech_stack` is an LLM-inferred field on the plan, not a directly settable parameter. The query is phrased to make the planner likely to set it (`"...including their website technology stack"`), but this is a nudge, not a guarantee.
- **`features.maps`** — Stage 7 (real Google Maps enrichment) is not part of this job runner's scope at all (see above). Requesting it produces a warning, not populated `maps_rating`/`maps_matched` fields.
- **`target.locations`** with more than one entry — the planner reads one geo phrase from the sentence, not N independent regional searches. Joined as an "or" phrase; flagged so a caller doesn't assume true multi-region coverage.

## Progress and cancellation, concretely

`progress_cb(stage, completed, total, message_code)` fires at:
- `"planning"` — pipeline start, and again once the plan resolves
- `"discover_scrape"` — once per discovery round (the natural checkpoint granularity of the existing multi-round/query-variation loop — not per individual candidate, to avoid destabilizing that already-intricate logic), and once after a "specific site" lookup completes
- `"finalizing"` — right before the job's result is assembled

`cancel_check()` is polled at the same checkpoints: before the expensive scrape phase starts, and at the top of every discovery round. A cancelled job never leaves a half-committed `storage/<domain>/` write — cancellation is checked *between* candidates, never mid-candidate (the per-round `asyncio.gather()` batch is never interrupted partway).

The heartbeat is a **separate** `asyncio` task ticking `last_heartbeat_at` every 15 seconds, independent of whether any progress event actually fired — so a job stuck waiting on a slow provider still proves it's alive, per the guide's own distinction between "progress" and "heartbeat."

## Status mapping

| `JobResult.status` | When |
|---|---|
| `completed` | Finished, shortfall is zero |
| `partially_completed` | Finished, `summary["shortfall"] > 0` (fewer qualified leads found than requested) |
| `awaiting_input` | The pipeline's clarification-question gate fired (`needs_clarification`) — matches the exact name the guide's own Section 3.6 frontend workflow table uses for this situation. Not in the guide's Section 4.4 result example, added because it's the honest state, not "failed" |
| `failed` | Moderation blocked the query, or the intent/planning stage failed |
| `cancelled` | `cancel_check()` returned true before the run finished |

## Idempotency-Key (POST /api/v1/pipeline/run)

`POST /api/v1/jobs` already had an idempotency key of its own — `job_id` (a duplicate submission is rejected with 409). The synchronous `POST /api/v1/pipeline/run` endpoint had none at all: a client retry (e.g. after a client-side timeout, with the server call having actually succeeded) would silently trigger a full second pipeline run. An optional `Idempotency-Key` header now closes that gap — a repeated call with the same key replays the exact prior outcome (same status code, same body, success **or** error) instead of re-executing. Implemented as an in-process dict in `api.py` (`_idempotency_store`), same honest limitation as the job store: **no TTL, no eviction, grows for the life of the process.**

**Concurrent (not just sequential) same-key requests**: a real gap found live — the store above only ever holds a *completed* result, so two requests with the same key arriving truly concurrently (both before either finishes) both passed the "nothing cached yet" check and both ran the full pipeline. Fixed with a second, separate in-process set (`_idempotency_inflight`) that reserves a key for the duration of an in-flight request; a concurrent duplicate now gets `409` instead of triggering a second run. Checked/set synchronously (no `await` between the check and the reservation), so it can't itself race within one process — but like the result cache, it's process-local, not a cross-process lock.

## Resume (POST /api/v1/jobs/{job_id}/resume)

Section 4.5's "Checkpoint" requirement accepts either "persist enough state to resume expensive work" **or** "clearly document which stages restart" — this implements the second, honestly, rather than faking the first.

`resume_job()` submits a **new** job (new `job_id`, supplied by the caller — Python never invents its own IDs) reusing the original job's `target`/`limits`/`features`. It does **not** replay progress from where the original stopped; it re-runs `run_pipeline()` from the beginning with the same translated query. What makes this a genuine resume rather than a plain re-run: any business the original job (or any prior job/CLI run against the same query) already committed to `storage/<domain>/` is skipped for free by the pipeline's own existing per-business cache (`docs/DATA_CONTRACTS.md`'s re-run/idempotency section) — a job that crashed after committing 40 of 50 requested businesses resumes by attempting only the remaining ~10, not all 50 again. This is coarse-grained (whole-business, not mid-scrape) resume — stated as such, not oversold as a fine-grained serialized checkpoint. The result's `resumed_from` field records the original `job_id` for lineage.

## Null/absence: the 5-way DataState distinction

Section 5's convention: *"Distinguish absent, unknown, not_attempted, not_applicable and provider_failure. Do not collapse them into empty strings."* `Prospect.score` carries an optional `state: DataState` field, populated only when `value` is `None`:
- `not_attempted` — `relevance_scoring` never produced a score for this candidate (e.g. the cached-reuse path)
- `unknown` — a genuine data-consistency surprise (no matching row, or an unparseable value) rather than an expected gap
- (`absent`/`not_applicable`/`provider_failure` are defined on the enum for other fields this codebase might extend to later — `Score` currently only produces the two states above, since those are the two real gaps `job_runner._build_result()` can actually distinguish today)

## What's still not built

- **No durable job store.** A process restart loses all in-flight and completed-but-unpolled job state. Acceptable for this handoff pass, not for production without a real backing store. The same is true of the Idempotency-Key store above.
- **No content hashing.** `ArtifactManifestEntry.sha256` is always `null` — no artifact in this codebase is hashed anywhere (see `ARTIFACTS_AND_STORAGE.md`).
- **Stages 3–8 are out of scope entirely**, as stated above — this is the single biggest gap between what's built here and full coverage of the guide's intent. Resume, idempotency, and DataState all apply only within that same Stage 1–2 scope.

## Verification

Manually exercised end-to-end (submit → poll progress → complete; submit → cancel mid-run; duplicate `job_id` rejection; unknown `job_id` → 404; resume; Idempotency-Key replay for both success and error outcomes) using faked pipeline functions and a real `httpx.AsyncClient` against the FastAPI app (not `TestClient` — its threaded event-loop portal produces misleading timing for background `asyncio.create_task()` work; a real single-event-loop async client matches actual `uvicorn` behavior). See `tests/mocked/test_job_lifecycle.py` (job_runner internals) and `tests/mocked/test_api_jobs.py` (the actual HTTP endpoints) for the same scenarios as automated, repeatable tests.
