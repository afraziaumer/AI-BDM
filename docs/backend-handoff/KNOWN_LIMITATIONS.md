# Known Limitations — Backend Handoff Scope

Scoped to what a Laravel/backend reviewer needs: hard-coded paths, patched packages, Windows dependencies, report/code discrepancies, and everything blocked on the deferred job-contract work (Section 4.3–4.6). For the full limitations list (Yelp/Trustpilot bot-protection, Roman Urdu language detection, ScrapingBee quota, etc.), see `../KNOWN_LIMITATIONS.md` — not repeated here to avoid the two copies drifting apart.

## Discrepancy: the Laravel guide assumes a non-LLM entry point exists — it doesn't

The Laravel Backend Construction Handoff Guide's header states `LLM STATUS: Not required for this handoff; future integration seam only`, and its Section 8 Python checklist asks to confirm *"the actual non-LLM entry point... is documented"* and that *"no hidden LLM requirement exists."*

**This premise is factually incorrect for this codebase, and was already corrected once before during this same handoff process** (an earlier draft doc, `Python_Code_Handoff_Guide_Current_Non_LLM_System.docx`, made the same claim and was superseded). Query planning, the clarification layer, route-planning's fallback, final reasoning, the relevance safety-net check, and moderation are all genuinely, unavoidably LLM-based (Groq) — confirmed live with real API calls and real cost tracking throughout this handoff. **There is no non-LLM alternative implementation for any of these stages.** The LLM requirement is not hidden — it's the opposite of hidden, it's openly required in `README.md`'s external-services table — but it does mean this checklist line item cannot be satisfied as literally written, and isn't being silently worked around here.

## Deferred: the job contract (Section 4.3–4.6)

By explicit decision of the Python team lead, the following is **out of scope for this handoff pass**, not forgotten:

- No `job_id` / `tenant_ref` / `correlation_id` / `schema_version` concept exists anywhere in this codebase.
- The request shape is a natural-language string (`{"query": "...", "concurrency": N}`), not the structured `target{industry, locations, company_size}` shape Section 4.3 describes.
- Results are written to CSV files and returned as a loosely-shaped summary dict, not the structured `JobResult` (`prospects[]`, `artifact_manifest`, `errors[]`) Section 4.4 describes.
- No progress emission, heartbeat, or cancellation mechanism exists (Section 4.5). A running pipeline call cannot be checked on mid-run or stopped early.
- No error taxonomy exists (Section 4.6). Failures surface as log lines and free-text strings (e.g. `summary["error"] = "intent_failed: ..."`, generic `HTTPException` details), not the canonical `{code, retryable, retry_after_seconds, correlation_id}` envelope.
- Consequently, the four schema files (`schemas/request.schema.json`, `progress.schema.json`, `result.schema.json`, `error.schema.json`) required by Section 7's folder structure are **not present in this folder** — they describe a contract that hasn't been designed yet. `JOB_EXECUTION.md` is likewise not present for the same reason.
- No content hashing (SHA-256) exists for any artifact (see `ARTIFACTS_AND_STORAGE.md`).
- `api.py` has no `/v1` prefix and no cursor pagination on `GET /leads` (Section 5's versioning/pagination conventions).

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
