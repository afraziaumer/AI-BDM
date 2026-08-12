# Laravel Quickstart: Running AI-BDM

This is the one document a Laravel developer needs to trigger the AI-BDM
pipeline and get results back -- no Python knowledge required. It covers
exactly two endpoints: `POST /api/v1/run-ai-bdm` and
`GET /api/v1/ai-bdm/{job_id}`.

These two routes are a **thin wrapper**, not a second pipeline. Under the
hood they call the same `job_runner.py` async job machinery that
`POST /api/v1/jobs` already exposes (see `JOB_EXECUTION.md`) -- they exist
only because that route wants a structured `{"target": {"industry": ...}}`
body, and this one just wants one sentence.

## Base URL

Wherever `api.py` is deployed, e.g. `http://your-python-host:8000`. All
routes are under `/api/v1`.

## Authentication

Every route below requires an API key header:

```
X-API-Key: <the AIBDM_API_KEY value the Python service was started with>
```

Missing or wrong key -> `401`. If the Python service has no key configured
at all, every protected route fails closed with `503` (never runs open).

## 1. Start a run

```
POST /api/v1/run-ai-bdm
X-API-Key: <key>
Content-Type: application/json

{"query": "5 marinas in Dubai with no CRM"}
```

`count` is optional -- omit it and the number in your sentence governs
("find 50 marinas..." -> 50). Pass it to override:

```json
{"query": "marinas in Dubai with no CRM", "count": 50}
```

**Response — `202 Accepted`:**

```json
{"success": true, "job_id": "aibdm_3f9a1c2b4d5e6f708192", "status": "queued"}
```

**Validation failure** (query missing or under 3 characters) — FastAPI's
standard `422` with a `detail` array, e.g.:

```json
{"detail": [{"type": "missing", "loc": ["body", "query"], "msg": "Field required"}]}
```

Check for a `2xx` status code rather than trying to parse a custom error
shape here — this is the same validation behavior every other route on
this API already uses (`PipelineRequest`, `JobRequest`, etc).

## 2. Poll for status/results

```
GET /api/v1/ai-bdm/{job_id}
X-API-Key: <key>
```

**While running (`200`):**

```json
{
  "success": true,
  "job_id": "aibdm_3f9a1c2b4d5e6f708192",
  "status": "running",
  "progress": {
    "stage": "discover_scrape",
    "completed": 12,
    "total": 20,
    "message": "round_complete"
  }
}
```

`progress` reflects exactly what the pipeline actually tracks -- one
checkpoint per discovery round, not per business. `completed`/`total` can
be `null` before the first checkpoint fires.

**Completed (`200`):**

```json
{
  "success": true,
  "job_id": "aibdm_3f9a1c2b4d5e6f708192",
  "status": "completed",
  "results": [
    {"company_name": "Acme Marina", "website": "https://acmemarina.com", "domain": "acmemarina.com", "score": 85}
  ]
}
```

`status` can also be `"partially_completed"` (fewer qualified leads found
than requested -- still a normal success, just check `results.length`) or
`"awaiting_input"` (the query was too vague to run at all -- see
`warnings` for what to ask the user).

**Failed (`200`, `success: false`):**

```json
{
  "success": false,
  "job_id": "aibdm_3f9a1c2b4d5e6f708192",
  "status": "failed",
  "error": "This request violates our usage policy and was not processed."
}
```

**Unknown job_id (`404`):**

```json
{"detail": "Unknown job_id: 'aibdm_bogus'"}
```

## cURL example

```bash
curl -X POST http://your-python-host:8000/api/v1/run-ai-bdm \
  -H "X-API-Key: $AIBDM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "5 marinas in Dubai with no CRM"}'

curl http://your-python-host:8000/api/v1/ai-bdm/aibdm_3f9a1c2b4d5e6f708192 \
  -H "X-API-Key: $AIBDM_API_KEY"
```

## Laravel example (Http facade)

```php
use Illuminate\Support\Facades\Http;

$response = Http::withHeaders([
    'X-API-Key' => config('services.ai_bdm.api_key'),
])->post(config('services.ai_bdm.base_url') . '/api/v1/run-ai-bdm', [
    'query' => '5 marinas in Dubai with no CRM',
]);

$jobId = $response->json('job_id');

// Later (e.g. from a queued job that polls, or a scheduled command):
$status = Http::withHeaders([
    'X-API-Key' => config('services.ai_bdm.api_key'),
])->get(config('services.ai_bdm.base_url') . "/api/v1/ai-bdm/{$jobId}");

if ($status->json('status') === 'completed') {
    $leads = $status->json('results');
}
```

`services.ai_bdm.api_key` / `services.ai_bdm.base_url` should come from
`.env` (`AI_BDM_API_KEY`, `AI_BDM_BASE_URL`), never hardcoded — same rule
this API's own `AIBDM_API_KEY` follows on the Python side.

## What this does NOT let Laravel do

There is no field anywhere in this contract for arbitrary code, shell
commands, or file paths. The only input is `query` (a string) and an
optional `count` (an integer, capped 1-500) — both pass through Pydantic
validation before anything runs. Laravel can ask "run AI-BDM for this
query," nothing else.

## CORS

Not configured, deliberately: CORS is a browser same-origin protection —
it only matters when JavaScript running in a user's browser calls this API
directly. A Laravel backend calling it server-side (`Http::post(...)` from
a controller/job, as in the example above) is never subject to CORS,
regardless of domain. If a browser-based client (e.g. an admin SPA) ever
needs to call this API directly instead of going through Laravel, add
`fastapi.middleware.cors.CORSMiddleware` at that point, scoped to that
specific origin — not before, and never `allow_origins=["*"]` alongside
`X-API-Key` auth.

## Known limitations (inherited from the underlying job runner)

- **In-process job store.** If the Python process restarts, in-flight and
  completed-but-unpolled jobs are gone — a `GET` for a job_id from before a
  restart returns `404`, indistinguishable from a job_id that never
  existed. No durable queue/Redis backing yet (see `JOB_EXECUTION.md`).
- **Stages 1-2 only.** This runs planning + discovery/scraping — the same
  scope `POST /api/v1/pipeline/run` and `POST /api/v1/jobs` already cover.
  Google Maps enrichment, tech-stack detection, and RAG-based final
  reasoning are separate stages in `main.py`'s CLI orchestration this job
  runner does not invoke. `results[].score` comes from `relevance_scoring`,
  not a Maps rating.
- **One tenant.** `run-ai-bdm` jobs are all submitted under a single fixed
  `tenant_ref` ("run-ai-bdm") — there's no per-Laravel-caller tenant
  isolation on this path today. Use `POST /api/v1/jobs` directly (the
  structured contract) if you need real multi-tenant `tenant_ref`/
  `correlation_id` tracking.
- **Run `api.py` with a single worker process.** Storage commits are safe
  across concurrent jobs *within* one process, but there is no cross-process
  file lock — deploying with multiple `uvicorn --workers` (or several
  overlapping processes) risks two workers committing the same business at
  nearly the same instant. See `docs/KNOWN_LIMITATIONS.md`'s
  `commit_domain()` entry. Scale by running one worker with normal async
  concurrency, not by adding more worker processes, until that gap is
  closed.
