# Known Limitations — Backend Handoff Scope

Scoped to what a Laravel/backend reviewer needs: hard-coded paths, patched packages, Windows dependencies, report/code discrepancies, and the job contract's real remaining gaps (Section 4.3–4.6 — now implemented, see below for what that does and doesn't cover). For the full limitations list (Yelp/Trustpilot bot-protection, Roman Urdu language detection, ScrapingBee quota, etc.), see `../KNOWN_LIMITATIONS.md` — not repeated here to avoid the two copies drifting apart.

## Discrepancy: the Laravel guide assumes a non-LLM entry point exists — it doesn't

The Laravel Backend Construction Handoff Guide's header states `LLM STATUS: Not required for this handoff; future integration seam only`, and its Section 8 Python checklist asks to confirm *"the actual non-LLM entry point... is documented"* and that *"no hidden LLM requirement exists."*

**This premise is factually incorrect for this codebase, and was already corrected once before during this same handoff process** (an earlier draft doc, `Python_Code_Handoff_Guide_Current_Non_LLM_System.docx`, made the same claim and was superseded). Query planning, the clarification layer, route-planning's fallback, final reasoning, the relevance safety-net check, and moderation are all genuinely, unavoidably LLM-based (Groq) — confirmed live with real API calls and real cost tracking throughout this handoff. **There is no non-LLM alternative implementation for any of these stages.** The LLM requirement is not hidden — it's the opposite of hidden, it's openly required in `README.md`'s external-services table — but it does mean this checklist line item cannot be satisfied as literally written, and isn't being silently worked around here.

## The job contract (Section 4.3–4.6) — implemented, with real remaining gaps

Built: `job_contracts.py` (versioned `JobRequest`/`ProgressEvent`/`JobResult`/`ErrorEnvelope`, an 8-class error taxonomy with `classify_error()`, a 5-way `DataState` null/absence distinction on `Score`), `job_translation.py` (structured request → the natural-language query the planner actually needs, with explicit warnings for fields that can't be honestly honored), `job_runner.py` (in-process job store, async execution, heartbeat, `resume_job()`), an in-process `Idempotency-Key` cache for `POST /api/v1/pipeline/run`, and `POST/GET /api/v1/jobs*` (including `/resume`) in `api.py`. Full design and status-mapping detail in `JOB_EXECUTION.md`.

**What's still genuinely missing, not silently papered over:**

- **Job store is in-process, not durable.** A process restart loses all job state — no Redis/Celery/database backing exists. The `Idempotency-Key` cache has the same limitation (no TTL/eviction either).
- **Stages 3–8 are out of this job runner's scope entirely.** It wraps `run_pipeline()` only (Stages 1–2) — the same scope the pre-existing `POST /api/v1/pipeline/run` already had. `features.maps`/`features.tech_stack` in a request produce a warning, not real Maps/tech-stack data. Resume, idempotency, and `DataState` all apply only within this same scope.
- **Resume is coarse-grained, not a serialized checkpoint.** `POST /api/v1/jobs/{job_id}/resume` re-runs from the start with the same target/limits/features under a new `job_id` — it relies on the pipeline's own existing per-business commit cache to skip already-done work, not a saved mid-run state. Satisfies Section 4.5's "clearly document which stages restart" alternative, not "persist enough state to resume."
- **No content hashing (SHA-256)** for any artifact — `ArtifactManifestEntry.sha256` is always `null` (see `ARTIFACTS_AND_STORAGE.md`).
- **Progress checkpoints are per discovery-round, not per-candidate** — a deliberate choice to avoid destabilizing the existing multi-round/query-variation discovery loop; see `JOB_EXECUTION.md` for why.
- **`DataState` is only wired up for `Score`.** The enum has 5 values; `job_runner._build_result()` currently only ever produces 2 of them (`not_attempted`, `unknown`) because those are the only two gaps it can actually distinguish today — `absent`/`not_applicable`/`provider_failure` are defined but not yet produced anywhere.

## Patched / vendored packages

`wappalyzer` is not pip-installed — PyPI was found to serve different, incompatible code under the same `wappalyzer==2.0.1` version string over time (confirmed live). A working, pre-patched copy is vendored directly into `vendor/wappalyzer/`; see that directory's `README.md` for the full incident history and update procedure. This is the only third-party package with in-repo patches — no other installed package is modified from its published form.

## Windows-specific dependencies

- `crawl_index.csv`'s `txt_path` column uses OS-native path separators (backslash on Windows) — any new code reading this column must split on `[\\/]`, not `/` alone.
- Cloudflare R2 object keys always use forward slashes regardless of host OS (`storage_sync.py` uses `Path.as_posix()`).
- Windows console defaults to cp1252 encoding; `main.py` reconfigures stdout/stderr to UTF-8 at startup to avoid `UnicodeEncodeError` on LLM-generated text (em-dashes, smart quotes).
- Developed and tested on Windows 11 only — not separately verified on Linux/macOS, though nothing is Windows-only by design.

## Report/code discrepancies

None currently known beyond the LLM-status one above. If a future review finds a docs claim that doesn't match actual code behavior, it belongs here — not silently corrected in the docs alone.

## Test coverage gaps (not blocked by the job contract)

- No dedicated unit/mocked test exists for `route_planner.plan_routes()` (Stage 3), `final_reasoning.answer_query()` (Stage 4), or the RAG embedding/retrieval pipeline (Stage 6) — see `PIPELINE_STAGES.md` for what each stage's test coverage actually is today.
- No test proves untrusted scraped page text cannot influence LLM tool configuration, file paths, credentials, or requested scope — `final_reasoning.py` passes raw page text into an LLM prompt; this is a real, currently-untested surface, not a hypothetical one. Section 4.9's "Security" test row asks for exactly this and it is not yet satisfied.
- No dedicated test proves duplicate-domain resolution is deterministic, even though the dedup logic itself exists in the discovery loop (`attempted`/`exclude_set` in `phase1_pipeline.run_pipeline()`).
